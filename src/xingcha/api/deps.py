"""请求级依赖：鉴权、限流、上下文。"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import Request

from ..services import auth as auth_svc
from ..services.auth import Principal
from ..services.ratelimit import RateLimiter

log = logging.getLogger(__name__)


@dataclass(slots=True)
class CallContext:
    """一次 ``/v1`` 调用的上下文。挂在 ``request.state.xc_ctx``。"""

    principal: Principal
    run_id: str


async def require_auth(request: Request) -> Principal:
    """校验 Bearer 令牌。

    **直通路径也走这里。** 一个不鉴权的 catch-all 反代 + 一把付费 key = 开放代理，
    是本项目唯一的「一天烧光余额」级事故。契约 §3.9 把它写成了冻结项，
    ``tests/test_passthrough.py`` 里有一条会红的断言守着它。
    """
    state = request.app.state.xc
    async with state.sessionmaker() as session:
        principal = await auth_svc.authenticate(session, request.headers.get("authorization"))
        # last_used_at 是尽力而为：它失败不该把一次成功的调用变成 500
        try:
            await auth_svc.touch_last_used(session, principal.token_id)
            await session.commit()
        except Exception:
            log.debug("更新 last_used_at 失败，忽略", exc_info=True)
            await session.rollback()
    return principal


def rate_limit_key(principal: Principal) -> str:
    """限流的主体是**令牌**而不是用户。

    v1 只有一个用户，按用户限流等于没限流；而按令牌限流在 v2 加多用户后语义不变，
    也让"某个客户端跑飞了"只影响它自己那把 key。
    """
    return principal.kid


def limiter_of(request: Request) -> RateLimiter:
    return request.app.state.xc.limiter
