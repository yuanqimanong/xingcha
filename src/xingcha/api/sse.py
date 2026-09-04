"""在**当前任务**里发送的流式响应。

------------------------------------------------------------------------------
为什么不能用 starlette 的 StreamingResponse
------------------------------------------------------------------------------

``StreamingResponse`` 会把响应体的迭代放进一个**子任务**（为了同时监听客户端断开）。
而 pydantic-ai 的并发闸是 anyio 的 ``CapacityLimiter``，它按 ``current_task()``
记账——acquire 与 release 必须在同一个任务里。

于是"先 ``anext()`` 探一次错、剩下的交给 StreamingResponse"这个写法会炸：第一次
拉取在请求任务里 acquire，之后的迭代在子任务里 release，直接

    RuntimeError: this borrower isn't holding any of this CapacityLimiter's tokens

而"先探一次错"这件事不能放弃：``run_stream()`` 的进入动作会真的发出上游请求，那一刻
状态码还没提交，连不上/401/模型不存在还能变成一个正常的 502 JSON。放弃它，这些故障
就只能表达成"200 之后一个空流"——OpenAI SDK 的调用方会拿到一个什么都不吐的迭代器，
既没有异常也没有内容，是最难排查的一种失败。

所以自己发。代价是失去 StreamingResponse 那个监听断开的子任务，用帧间的
``is_disconnected()` `轮询补上——SSE 帧很密，两者的响应速度差别可以忽略。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from starlette.requests import Request
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

log = logging.getLogger(__name__)


class SameTaskEventStream(Response):
    """SSE 响应：在处理请求的那个任务里逐帧 send。"""

    media_type = "text/event-stream"

    def __init__(self, frames: AsyncIterator[bytes], *, request: Request) -> None:
        # **故意不调 super().__init__()。** ``Response.__init__`` 会把 content=None
        # 渲染成 ``b""`` 并据此发出 ``Content-Length: 0``，真实 HTTP 客户端于是一个
        # 字节都不读——流式响应变成空响应，而且不报错。
        #
        # starlette 的 StreamingResponse 靠"不存在 body 属性"绕开这一点
        # （``init_headers`` 里是 ``getattr(self, "body", None)``），这里照做。
        #
        # 注意 TestClient 不检查 content-length，这个 bug 只有真服务器 + 真客户端
        # 才暴露——所以 test_openai_sdk 那组用真 uvicorn 跑不是多余的。
        self.status_code = 200
        self.background = None
        self.init_headers(None)
        self._frames = frames
        self._request = request

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": self.status_code,
                "headers": self.raw_headers,
            }
        )
        try:
            async for chunk in self._frames:
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
                # 客户端走了就别再往上游要了——那是在为没人看的字节付钱。
                if await self._request.is_disconnected():
                    log.info("客户端在流式响应中途断开，停止拉取上游")
                    return
        finally:
            aclose = getattr(self._frames, "aclose", None)
            if aclose is not None:
                await aclose()
            await send({"type": "http.response.body", "body": b"", "more_body": False})
