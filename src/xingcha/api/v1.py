"""``/v1`` 的装配。

**注册顺序是这个文件存在的理由**，写错会以静默的方式坏掉：

1. ``OPTIONS`` —— 免鉴权（浏览器预检不带 Authorization），且必须在 catch-all 之前
2. 自有路径（``/models``、``/models/{id}``、``/chat/completions``）—— 带鉴权与限流
3. catch-all 直通 —— 带鉴权与限流，**必须最后**，否则会把上面两组全吞掉

鉴权用 ``yield`` 依赖而不是中间件：Starlette 的中间件里抛出的异常**不会**经过
FastAPI 的异常处理器，于是一个本该是 401 的失败会变成 500，错误契约当场失效。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Request

from ..services.auth import Principal
from ..services.inflight import InflightRegistry
from ..services.ratelimit import RateLimiter
from . import openai_compat, passthrough
from .deps import rate_limit_key, require_auth

log = logging.getLogger(__name__)


async def authed_and_limited(
    request: Request,
    principal: Principal = Depends(require_auth),
) -> AsyncIterator[Principal]:
    """鉴权 + 限流 + 在飞登记。**直通路径也走这里。**

    ``finally`` 里的释放不能省：在飞计数泄漏的表现是某个 token 越用越慢直到完全被拒，
    而且只有重启能恢复——很难联想到根因。

    在飞登记挂在同一个 ``finally`` 上，而不是挂在 ``RunTracker.submit`` 上。submit 在
    Agent 非流式路径上只出现在成功分支和 ``fail()``（只接 ``XingchaError``）里，挂那儿
    等于「冒出个别的异常就永远留一条」，而总览页上一条不存在的「正在跑」会让管理员
    永远不敢升级。这里的 ``finally`` 是整条 ``/v1`` 上唯一无条件会走到的地方。
    """
    state = request.app.state.xc
    limiter: RateLimiter = state.limiter
    inflight: InflightRegistry = state.inflight
    key = rate_limit_key(principal)
    await limiter.acquire(key)
    request.state.principal = principal
    # 登记在 acquire 之后：被限流拒掉的请求没在跑，不该出现在「正在跑」里。
    request.state.inflight_ticket = inflight.enter(token_name=principal.token_name)
    try:
        yield principal
    finally:
        inflight.leave(request.state.inflight_ticket)
        await limiter.release(key)


def build_router() -> APIRouter:
    root = APIRouter(prefix="/v1")

    # 1 · OPTIONS 免鉴权，且先于 catch-all。
    root.include_router(openai_compat.options_router)

    # 2 · 自有路径。
    root.include_router(openai_compat.router, dependencies=[Depends(authed_and_limited)])

    # 3 · catch-all 直通。必须最后。
    root.include_router(passthrough.router, dependencies=[Depends(authed_and_limited)])

    return root
