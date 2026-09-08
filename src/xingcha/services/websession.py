"""管理后台的登录会话与 CSRF。

与 API 令牌**完全分开**：``sk-xc-`` 是给机器用的、走 Bearer 头；后台会话是给浏览器
用的、走 SameSite=Strict 的 cookie。混用会让一把泄漏的 API key 直接拿到后台权限，
而后台里有一个能改写上游 base_url 的设置页——那等于把付费 key 交出去。

CSRF 防护是三层叠加，任何一层单独都不够：

1. ``SameSite=Strict`` —— 挡住绝大多数跨站请求，但老浏览器与某些边缘情形会漏
2. **double-submit token** —— 表单里的隐藏字段必须与会话里的值匹配；攻击者的页面
   读不到我们的 cookie，也就凑不出这个字段
3. ``Origin`` / ``Sec-Fetch-Site`` 校验 —— 现代浏览器一定会带，能挡住 1 和 2 的漏网

这是准入项 A1：没有它们，攻击者只需让管理员的浏览器 POST 一次把上游 base_url 指向
自己，下一次调用就把付费 key 送上门。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import contract as C
from ..db.models import User, WebSession, utcnow

log = logging.getLogger(__name__)

SESSION_COOKIE = "xc_session"
CSRF_FIELD = "csrf_token"
CSRF_HEADER = "x-csrf-token"

_hasher = PasswordHasher()


class LoginRateLimited(RuntimeError):
    """登录尝试过于频繁。

    公网上的管理后台会被撞库。指数退避让在线爆破变得不划算，同时不影响正常人
    偶尔输错一次。
    """

    def __init__(self, wait_seconds: float) -> None:
        super().__init__(f"尝试过于频繁，请 {wait_seconds:.0f} 秒后再试。")
        self.wait_seconds = wait_seconds


@dataclass
class _Attempts:
    count: int = 0
    blocked_until: float = 0.0


class LoginThrottle:
    """按用户名的登录退避。进程内内存实现（依赖单 worker）。"""

    def __init__(self, *, threshold: int = 5, base_seconds: float = 2.0) -> None:
        self._threshold = threshold
        self._base = base_seconds
        self._state: dict[str, _Attempts] = {}

    def check(self, key: str) -> None:
        st = self._state.get(key)
        if st and st.blocked_until > time.monotonic():
            raise LoginRateLimited(st.blocked_until - time.monotonic())

    def record_failure(self, key: str) -> None:
        st = self._state.setdefault(key, _Attempts())
        st.count += 1
        if st.count >= self._threshold:
            # 指数退避，封顶 15 分钟——再长就变成了一个拒绝服务的开关
            delay = min(self._base * 2 ** (st.count - self._threshold), 900.0)
            st.blocked_until = time.monotonic() + delay
            log.warning("登录失败 %d 次，暂停 %.0f 秒（key=%s）", st.count, delay, key)

    def record_success(self, key: str) -> None:
        self._state.pop(key, None)


# --------------------------------------------------------------------------
# 密码
# --------------------------------------------------------------------------


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def env_password_usable(env_password: str | None) -> bool:
    """环境变量里那个密码本身合不合格（不管它最终是否生效）。

    低于长度下限**一律拒用**，而不是"警告后放过"：一个 4 位的后台密码在公网机器上
    是实打实的洞，而这个功能的全部意义是方便——方便不该以此为代价。

    拒用之后会回落到库里的密码（或首次设密流程），所以用户不会被锁在门外，
    只是那条捷径不生效。日志里会说清原因。
    """
    if not env_password:
        return False
    if len(env_password) < C.MIN_ADMIN_PASSWORD_LEN:
        log.error(
            "环境变量 XINGCHA_ADMIN_PASSWORD 被忽略：长度 %d 不足 %d 位。"
            "后台密码守着上游 key 与全部配置，太短的话这个便利不值得。"
            "改长一点，或删掉它改用后台的首次设密流程。",
            len(env_password),
            C.MIN_ADMIN_PASSWORD_LEN,
        )
        return False
    return True


def env_password_in_effect(stored: str | None, env_password: str | None) -> bool:
    """环境变量那个密码**此刻是否真的在生效**。

    ------------------------------------------------------------------------
    优先级：先立者为准
    ------------------------------------------------------------------------

    库里已经有密码 → **库赢**，环境变量被忽略。
    库里没有密码 + 环境变量合格 → 用环境变量。

    反过来（环境变量总是优先）会引入一个真实的越权路径：任何能往 ``.env`` 写一行
    的人——一次误挂的卷、一个共享的部署目录、一个能写文件的漏洞——就能顶掉已经
    建好的管理员密码。"先立者为准"让这条路走不通：密码一旦在库里立起来，只有
    握着它的人（或显式的 ``admin reset-password``）能改。

    代价是"改 .env 里的密码不生效"这件事必须说清楚，否则用户会以为改了。
    所以启动时会打一条日志，登录页与设置页也都有说明。
    """
    return not stored and env_password_usable(env_password)


def verify_admin_password(
    stored: str | None, password: str, env_password: str | None = None
) -> bool:
    """校验后台密码。**这是唯一的判定点。**

    优先级见 :func:`env_password_in_effect`：库里有就用库里的，没有才看环境变量。
    绝不"两个都能用"——那种状态没人说得清哪个才是真的，而"我改了密码但旧的还能登"
    是最坏的一种安全体验。

    环境变量那条用 ``compare_digest`` 而不是 argon2：手上是明文，没有哈希可验，
    而普通的 ``==`` 会按字符逐位短路，泄漏前缀长度。
    """
    if env_password_in_effect(stored, env_password):
        return secrets.compare_digest(password, env_password or "")
    return verify_password(stored, password)


def verify_password(stored: str | None, password: str) -> bool:
    """校验密码。

    ``stored`` 为空时**仍然走一次哈希计算**再返回 False：直接返回会让"这个用户
    没设密码"变成一个可测的时序差异。
    """
    if not stored:
        _hasher.hash(password)  # 恒定工作量，避免时序泄漏
        return False
    try:
        return _hasher.verify(stored, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def needs_rehash(stored: str) -> bool:
    try:
        return _hasher.check_needs_rehash(stored)
    except InvalidHashError:
        return True


# --------------------------------------------------------------------------
# 会话
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NewSession:
    """新建会话。两个明文值只在这一刻存在，库里只有它们的哈希。"""

    token: str
    csrf: str
    expires_at: str


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def create(session: AsyncSession, user_id: int, *, ttl_hours: int) -> NewSession:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    expires = (datetime.now(UTC) + timedelta(hours=ttl_hours)).isoformat(timespec="seconds")
    session.add(
        WebSession(
            id=_sha(token),
            user_id=user_id,
            csrf_hash=_sha(csrf),
            expires_at=expires,
            created_at=utcnow(),
        )
    )
    return NewSession(token=token, csrf=csrf, expires_at=expires)


async def resolve(session: AsyncSession, token: str | None) -> WebSession | None:
    """按 cookie 取会话。过期的顺手删掉。"""
    if not token:
        return None
    row = (
        await session.execute(select(WebSession).where(WebSession.id == _sha(token)))
    ).scalar_one_or_none()
    if row is None:
        return None
    if row.expires_at <= utcnow():
        await session.delete(row)
        return None
    return row


def csrf_matches(row: WebSession, submitted: str | None) -> bool:
    """double-submit 比对。常量时间。"""
    if not submitted:
        return False
    return hmac.compare_digest(row.csrf_hash, _sha(submitted))


async def destroy(session: AsyncSession, token: str | None) -> None:
    if not token:
        return
    await session.execute(delete(WebSession).where(WebSession.id == _sha(token)))


async def purge_expired(session: AsyncSession) -> int:
    result = await session.execute(delete(WebSession).where(WebSession.expires_at <= utcnow()))
    return getattr(result, "rowcount", 0) or 0


async def revoke_all(session: AsyncSession) -> int:
    """吊销所有后台会话，返回被吊销的条数。

    改密码与重置密码都要调。不调的话，一个已登录的浏览器仍然握着完整权限——
    而这两个操作的场景往往正是"我不确定还有谁登着"。
    """
    from sqlalchemy import delete, func, select

    n = (await session.execute(select(func.count()).select_from(WebSession))).scalar() or 0
    await session.execute(delete(WebSession))
    return int(n)


async def get_admin(session: AsyncSession) -> User | None:
    return (await session.execute(select(User).where(User.id == 1))).scalar_one_or_none()


async def has_password(session: AsyncSession) -> bool:
    """是否已完成首次设密。

    未设密时后台只暴露一个"设置管理员密码"的向导，其余页面全部拒绝——否则首次部署
    到设密之间的窗口里，后台是完全敞开的。
    """
    admin = await get_admin(session)
    return bool(admin and admin.password_hash)
