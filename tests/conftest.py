"""测试夹具。

**假上游是一个真实的本地 HTTP 服务器**，不是 mock。直通层要验证的恰恰是字节级
转发与流式行为，而 mock transport 会把响应物化，测不出"帧有没有被合并成一坨"
这类问题——那正是流式最容易坏的地方。

它同时记录收到的每一个请求，让"无鉴权时上游有没有被打到"成为一条可断言的事实
而不是推测。
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from xingcha.config import Settings


@dataclass
class RecordedRequest:
    method: str
    path: str
    query: str
    headers: dict[str, str]
    body: bytes


@dataclass
class FakeUpstream:
    """假的 OpenRouter。"""

    port: int
    requests: list[RecordedRequest] = field(default_factory=list)
    server: Any = None
    #: Agent 路径按顺序返回的工具调用负载。最后一个会被重复使用。
    tool_payloads: list[Any] = field(default_factory=list)
    tool_call_index: int = 0
    #: T1 档下上游实际收到的 response_format，用于断言 schema 被怎样改写。
    native_requests: list[Any] = field(default_factory=list)
    #: 非 None 时，每条响应的 usage 里带上这个 ``cost``（模拟中转报账单）。
    #: 默认不带——不是所有中转都报，"不报时回落目录价"也必须被测到。
    report_cost: str | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def reset(self) -> None:
        self.requests.clear()
        self.tool_payloads = []
        self.tool_call_index = 0
        self.native_requests = []
        self.report_cost = None

    @property
    def hit_count(self) -> int:
        return len(self.requests)

    @property
    def chat_count(self) -> int:
        """只数模型调用。

        ``hit_count`` 里混着启动时的目录预热（``/v1/models``），拿它断言"模型被调了
        几次"会莫名多一次。
        """
        return sum(1 for r in self.requests if r.path.endswith("/chat/completions"))

    def last(self) -> RecordedRequest:
        assert self.requests, "上游一次都没有被调用"
        return self.requests[-1]


def _build_upstream_app(state: FakeUpstream) -> FastAPI:
    app = FastAPI()

    def usage(**counts: Any) -> dict[str, Any]:
        """构造 usage 块，按需附上中转报的费用。"""
        if state.report_cost is not None:
            counts["cost"] = float(state.report_cost)
        return counts

    @app.middleware("http")
    async def record(request: Request, call_next):
        body = await request.body()
        state.requests.append(
            RecordedRequest(
                method=request.method,
                path=request.url.path,
                query=request.url.query,
                headers={k.lower(): v for k, v in request.headers.items()},
                body=body,
            )
        )
        return await call_next(request)

    @app.get("/v1/models")
    async def models() -> dict[str, Any]:
        return {
            "data": [
                {
                    "id": "openai/gpt-5",
                    "name": "OpenAI: GPT-5",
                    "created": 1700000000,
                    "supported_parameters": ["tools", "structured_outputs", "response_format"],
                    "pricing": {
                        "prompt": "0.00000125",
                        "completion": "0.00001",
                        "input_cache_read": "0.000000125",
                    },
                },
                {
                    # 只有 response_format 没有 structured_outputs —— 判档必须区分这两者
                    "id": "vendor/no-native",
                    "name": "No Native Schema",
                    "created": 1700000001,
                    "supported_parameters": ["response_format"],
                    "pricing": {"prompt": "0.000001", "completion": "0.000002"},
                },
            ]
        }

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        payload = await request.json()

        # T1 / T1+ 走 response_format 通道：回一条 JSON 文本而不是工具调用。
        # 顺带记下线上实际收到的 required，让测试能断言"T1 确实改写了 schema"。
        rf = payload.get("response_format") or {}
        # T3 用 json_object（无 schema 约束），T1/T1+ 用 json_schema。两者都回 JSON 文本。
        if rf.get("type") in ("json_schema", "json_object"):
            import json as _json

            if rf.get("type") == "json_schema":
                state.native_requests.append(rf)
            queue = state.tool_payloads or [{"ok": True}]
            body = queue[min(state.tool_call_index, len(queue) - 1)]
            state.tool_call_index += 1
            return JSONResponse(
                {
                    # id 必须每次不同：费用是按响应 id 关联回来的，固定 id 会让
                    # 两次不相关的调用互相串账。
                    "id": f"chatcmpl-n{state.tool_call_index}",
                    "object": "chat.completion",
                    "created": 0,
                    "model": payload.get("model"),
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": _json.dumps(body, ensure_ascii=False),
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": usage(prompt_tokens=20, completion_tokens=8, total_tokens=28),
                }
            )

        # Agent（T2）走 tool 通道：请求里带 tools 时必须回一个 tool_call，
        # 否则 pydantic-ai 会当成"模型没按要求输出"而重试，测出来的次数全是错的。
        if payload.get("tools"):
            import json as _json

            queue = state.tool_payloads or [{"ok": True}]
            body = queue[min(state.tool_call_index, len(queue) - 1)]
            state.tool_call_index += 1
            return JSONResponse(
                {
                    "id": f"chatcmpl-t{state.tool_call_index}",
                    "object": "chat.completion",
                    "created": 0,
                    "model": payload.get("model"),
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": f"call-{state.tool_call_index}",
                                        "type": "function",
                                        "function": {
                                            "name": payload["tools"][0]["function"]["name"],
                                            "arguments": _json.dumps(body),
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": usage(prompt_tokens=12, completion_tokens=6, total_tokens=18),
                }
            )

        if payload.get("stream"):

            async def gen():
                for i in range(3):
                    yield f'data: {{"choices":[{{"delta":{{"content":"{i}"}}}}]}}\n\n'.encode()
                    await asyncio.sleep(0.005)
                import json as _json

                tail = {
                    "model": "openai/gpt-5",
                    "choices": [],
                    "usage": usage(prompt_tokens=10, completion_tokens=5),
                }
                yield f"data: {_json.dumps(tail)}\n\n".encode()
                yield b"data: [DONE]\n\n"

            return StreamingResponse(
                gen(),
                media_type="text/event-stream",
                headers={"set-cookie": "upstream=1", "x-request-id": "up-1"},
            )
        # 字段必须齐：OpenAI SDK 会按 ChatCompletion 模型校验响应，缺 created 或
        # finish_reason 会被判为无效响应——假上游不逼真的话，测出来的是假的失败。
        return JSONResponse(
            {
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "created": 1700000000,
                "model": payload.get("model"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "hi"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": usage(
                    prompt_tokens=10,
                    completion_tokens=5,
                    total_tokens=15,
                    prompt_tokens_details={"cached_tokens": 4},
                ),
            },
            headers={"set-cookie": "upstream=1", "x-request-id": "up-1"},
        )

    @app.post("/v1/embeddings")
    async def embeddings() -> dict[str, Any]:
        return {"object": "list", "data": [{"embedding": [0.1, 0.2]}]}

    @app.get("/v1/models/{author}/{slug}/endpoints")
    async def endpoints(author: str, slug: str) -> dict[str, Any]:
        return {"data": {"id": f"{author}/{slug}", "endpoints": ["a"]}}

    return app


@pytest.fixture(scope="session")
def upstream() -> Iterator[FakeUpstream]:
    state = FakeUpstream(port=8893)
    app = _build_upstream_app(state)
    config = uvicorn.Config(app, host="127.0.0.1", port=state.port, log_level="error")
    server = uvicorn.Server(config)
    state.server = server
    threading.Thread(target=server.run, daemon=True).start()

    for _ in range(100):
        if getattr(server, "started", False):
            break
        time.sleep(0.05)
    else:  # pragma: no cover
        raise RuntimeError("假上游没能启动")

    yield state
    server.should_exit = True


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        request_timeout=10.0,
        catalog_ttl_seconds=60,
    )
