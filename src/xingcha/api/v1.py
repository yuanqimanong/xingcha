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
from ..services.ratelimit import RateLimiter
from . import openai_compat, passthrough
from .deps import rate_limit_key, require_auth

log = logging.getLogger(__name__)


async def authed_and_limited(
    request: Request,
    principal: Principal = Depends(require_auth),
) -> AsyncIterator[Principal]:
    """鉴权 + 限流。**直通路径也走这里。**

    ``finally`` 里的释放不能省：在飞计数泄漏的表现是某个 token 越用越慢直到完全被拒，
    而且只有重启能恢复——很难联想到根因。
    """
    limiter: RateLimiter = request.app.state.xc.limiter
    key = rate_limit_key(principal)
    await limiter.acquire(key)
    request.state.principal = principal
    try:
        yield principal
    finally:
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
