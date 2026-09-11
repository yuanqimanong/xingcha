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


# **密码**


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def normalize_env_password(env_password: str | None) -> str:
    """把环境变量里的密码归一化。**空 / 只有空白 = 没设置。**

    ``.env`` 里写 ``XINGCHA_ADMIN_PASSWORD=`` （键在、值空）是最常见的形态——
    ``.env.example`` 抄过来就是这样。它的意思显然是"我还没填"，所以必须与
    "填了一个坏值"区分开：前者不该有任何抱怨，后者必须报出来。

    两头的空白一并去掉：``.env`` 的解析对首尾空白本来就不可靠，而一个首尾带空格的
    密码是纯粹的陷阱——你按看到的字符输入，永远登不进去。

    归一化只有这一处，登录校验与"设了没"的判断都走它。分成两份的话会出现最难查的
    那种状态：**判断说设了、校验却对不上**，于是没人能登进去而日志说一切正常。
    """
    return (env_password or "").strip()


def env_password_usable(env_password: str | None) -> bool:
    """环境变量里那个密码算不算"设了"。**任意非空即生效，不设长度门槛。**

    这是一条经过一次决定的放宽。原先非空但短于 :data:`MIN_ADMIN_PASSWORD_LEN`
    会被**拒用**——理由是后台能改写上游 ``base_url``，等于能把付费 key 指到任意
    地址，所以弱密码不是"方便"而是洞。但拒用带来的实际后果是：用户在 .env 里写了
    一行、重启、发现还是要走首次设密，而这条捷径的**全部意义就是省掉那个流程**。

    现在的取法是：照用，但在启动时警告一次（见 ``app._log_password_source``）。
    强度的判断交给用户，我们只保证他知道自己选了什么。

    注意这不影响**浏览器首次设密**那条路径——那里仍然要求
    :data:`MIN_ADMIN_PASSWORD_LEN` 位。两处的差别是有意的：环境变量是运维自己写在
    自己机器上的文件里，而表单是任何能打开这一页的人在设。
    """
    return bool(normalize_env_password(env_password))


def env_password_is_weak(env_password: str | None) -> bool:
    """生效了，但短于建议下限。只用来决定"要不要在启动时提一句"。"""
    normalized = normalize_env_password(env_password)
    return bool(normalized) and len(normalized) < C.MIN_ADMIN_PASSWORD_LEN


def env_password_in_effect(stored: str | None, env_password: str | None) -> bool:
    """环境变量那个密码**此刻是否真的在生效**。

    **优先级：先立者为准**

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
        return secrets.compare_digest(password, normalize_env_password(env_password))
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


# **会话**


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
