"""令牌签发与校验。

明文格式与演进规则见 :mod:`xingcha.contract` §3.2。这里只讲实现上的三个要点：

**按 kid 查表，不按 hash 查表。**
    很自然的做法是把 ``sha256(secret)`` 当查表键——省一列。但那样一来，将来换成带盐的
    argon2id 就**无法反查**（每行盐不同，算不出查表值），只能全表逐行 verify，
    ``O(n)`` 次 argon2 每请求 = 送上门的 DoS。那一刻「已签发 key 永不失效」的承诺就破了。
    ``kid`` 是独立的、不可推导的标识，与哈希方案完全解耦。

**常量时间比较。**
    ``==`` 会在第一个不同字节处返回，泄漏前缀匹配长度。虽然对 sha256 输出做时序攻击
    在网络抖动下很难成功，但这是一行代码的事。

**对外不区分失败原因。**
    无效 / 禁用 / 过期一律回同一条消息。区分等于给公网一个 token 有效性 oracle——
    "这个 key 存在但过期了"是白送的信息。区分只进日志。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import contract as C
from ..db.models import Token, utcnow
from ..errors import InvalidApiKey

log = logging.getLogger(__name__)

#: kid 的字符集。契约里 kid 是 ``[0-9a-z]{16}``。
_KID_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"


@dataclass(frozen=True, slots=True)
class IssuedToken:
    """新签发的令牌。``plaintext`` **只在这一刻存在**，之后不可恢复。"""

    plaintext: str
    kid: str
    display_prefix: str
    token_id: int
    name: str


@dataclass(frozen=True, slots=True)
class Principal:
    """通过鉴权的调用方身份。"""

    user_id: int
    token_id: int
    kid: str
    token_name: str


def _new_kid() -> str:
    return "".join(secrets.choice(_KID_ALPHABET) for _ in range(C.TOKEN_KID_LEN))


def _hash_secret(secret: str, alg: str = C.TOKEN_SCHEME_1_ALG) -> str:
    if alg == "sha256":
        return hashlib.sha256(secret.encode("ascii")).hexdigest()
    raise ValueError(f"未知的哈希算法 {alg!r}")


def _verify_secret(secret: str, stored_hash: str, alg: str) -> bool:
    """常量时间比较。

    未来加 scheme=2（argon2id）时在这里加分支，**不要删掉 sha256 分支**——
    删掉就等于让所有 scheme=1 的已签发 key 一起失效。
    """
    if alg == "sha256":
        return hmac.compare_digest(_hash_secret(secret, "sha256"), stored_hash)
    log.error("令牌 hash_alg=%r 不被当前版本支持", alg)
    return False


async def issue(
    session: AsyncSession,
    *,
    name: str,
    user_id: int = 1,
    expires_at: str | None = None,
) -> IssuedToken:
    """签发一把新令牌。返回值里的明文**只此一次可见**。"""
    # kid 撞车的概率是 36^-16，但唯一索引在那儿，撞了就重试比事后排查便宜
    for _ in range(5):
        kid = _new_kid()
        exists = (
            await session.execute(select(Token.id).where(Token.kid == kid))
        ).scalar_one_or_none()
        if exists is None:
            break
    else:  # pragma: no cover - 概率上不可达
        raise RuntimeError("连续 5 次生成的 kid 都已存在，这不该发生")

    secret = secrets.token_urlsafe(32)
    plaintext = f"{C.TOKEN_PREFIX}{C.TOKEN_SCHEME_CURRENT}-{kid}-{secret}"
    # 生成的东西必须符合自己冻结的契约。这条断言在开发期就会抓住格式漂移，
    # 而不是等到某个客户端拿着一把不合法的 key 来报障。
    assert C.TOKEN_ENVELOPE_RE.match(plaintext), f"生成的令牌不符合契约：{plaintext[:20]}…"

    row = Token(
        user_id=user_id,
        name=name,
        scheme=C.TOKEN_SCHEME_CURRENT,
        kid=kid,
        hash=_hash_secret(secret),
        hash_alg=C.TOKEN_SCHEME_1_ALG,
        kdf_params=None,
        display_prefix=C.token_display_prefix(C.TOKEN_SCHEME_CURRENT, kid),
        is_active=True,
        expires_at=expires_at,
        created_at=utcnow(),
    )
    session.add(row)
    await session.flush()

    return IssuedToken(
        plaintext=plaintext,
        kid=kid,
        display_prefix=row.display_prefix,
        token_id=row.id,
        name=name,
    )


async def authenticate(session: AsyncSession, header_value: str | None) -> Principal:
    """校验 ``Authorization`` 头，失败一律抛 :class:`InvalidApiKey`。

    每一个 ``raise`` 都带 ``log_detail`` 说明真实原因——对外一致、对内可查。
    """
    if not header_value:
        raise InvalidApiKey(log_detail="缺少 Authorization 头")

    scheme, _, raw = header_value.partition(" ")
    if scheme.lower() != C.AUTH_SCHEME or not raw:
        raise InvalidApiKey(log_detail=f"Authorization 头的 scheme 是 {scheme!r}，期望 Bearer")

    m = C.TOKEN_ENVELOPE_RE.match(raw.strip())
    if m is None:
        raise InvalidApiKey(log_detail="令牌格式不符合信封正则")

    kid = m.group("kid")
    secret = m.group("secret")
    scheme_num = int(m.group("scheme"))

    if scheme_num not in C.TOKEN_SCHEMES_SUPPORTED:
        raise InvalidApiKey(log_detail=f"令牌 scheme={scheme_num} 不被本版本支持")

    row = (await session.execute(select(Token).where(Token.kid == kid))).scalar_one_or_none()
    if row is None:
        raise InvalidApiKey(log_detail=f"kid={kid} 不存在")

    if not _verify_secret(secret, row.hash, row.hash_alg):
        raise InvalidApiKey(log_detail=f"kid={kid} 的 secret 不匹配")

    if not row.is_active:
        raise InvalidApiKey(log_detail=f"kid={kid} 已禁用")

    if row.expires_at and row.expires_at <= utcnow():
        raise InvalidApiKey(log_detail=f"kid={kid} 已于 {row.expires_at} 过期")

    return Principal(user_id=row.user_id, token_id=row.id, kid=row.kid, token_name=row.name)


async def touch_last_used(session: AsyncSession, token_id: int) -> None:
    """更新 ``last_used_at``。

    刻意做成一次独立的、可以失败的写：它不该阻塞请求路径，更不该让一次写冲突把
    一个本来成功的调用变成 500。
    """
    row = await session.get(Token, token_id)
    if row is not None:
        row.last_used_at = utcnow()


async def revoke(session: AsyncSession, kid: str) -> bool:
    """吊销令牌。置 ``is_active=False`` 而不是删行——删掉之后历史 run 就找不到归属了。"""
    row = (await session.execute(select(Token).where(Token.kid == kid))).scalar_one_or_none()
    if row is None or not row.is_active:
        return False
    row.is_active = False
    return True


async def list_tokens(session: AsyncSession, *, user_id: int | None = None) -> list[Token]:
    stmt = select(Token).order_by(Token.created_at.desc())
    if user_id is not None:
        stmt = stmt.where(Token.user_id == user_id)
    return list((await session.execute(stmt)).scalars().all())


def is_expired(row: Token) -> bool:
    return bool(row.expires_at and row.expires_at <= utcnow())


def parse_expiry(days: int | None) -> str | None:
    if days is None:
        return None
    from datetime import timedelta

    return (datetime.now(UTC) + timedelta(days=days)).isoformat(timespec="microseconds")
