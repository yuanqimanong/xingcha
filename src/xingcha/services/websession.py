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


async def get_admin(session: AsyncSession) -> User | None:
    return (await session.execute(select(User).where(User.id == 1))).scalar_one_or_none()


async def has_password(session: AsyncSession) -> bool:
    """是否已完成首次设密。

    未设密时后台只暴露一个"设置管理员密码"的向导，其余页面全部拒绝——否则首次部署
    到设密之间的窗口里，后台是完全敞开的。
    """
    admin = await get_admin(session)
    return bool(admin and admin.password_hash)
