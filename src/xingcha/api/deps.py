"""请求级依赖：鉴权与限流主体。"""

from __future__ import annotations

import logging

from fastapi import Request

from ..services import auth as auth_svc
from ..services.auth import Principal

log = logging.getLogger(__name__)


async def require_auth(request: Request) -> Principal:
    """校验 Bearer 令牌。

    **直通路径也走这里。** 一个不鉴权的 catch-all 反代 + 一把付费 key = 开放代理，
    是本项目唯一的「一天烧光余额」级事故。契约 §8 把它写成了冻结项
    （``PASSTHROUGH_REQUIRES_AUTH``）。
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
