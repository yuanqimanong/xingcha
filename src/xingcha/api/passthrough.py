"""裸模型透明直通。

**两份既有文档从头到尾没有这一层**——它们把星槎设想成"只暴露 Agent"的控制面。
但用户的真实痛点是"本地不挂代理就能用任意模型"，所以 ``/v1`` 下所有非自有路径
原样反代到上游，包括流式。

这一层刻意做得**很笨**：除了识别路径与记录用量，它不解析任何东西。笨是优点——
OpenRouter 明天加一个新端点、新参数、新的响应字段，这里都不需要改。

安全上它是本项目风险最高的一段：一个 catch-all 反代后面挂着一把付费 key。
下面每条卫生措施对应契约 §3.9 的一行，都不是可选项。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping

import httpx2
from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse

from .. import contract as C
from ..core.upstream import UpstreamNotConfigured
from ..errors import RequestTooLarge, UpstreamError, UpstreamTimeout, XingchaError
from .runlog_mw import RunTracker

log = logging.getLogger(__name__)

router = APIRouter()


class PathRejected(ValueError):
    """路径穿越或形状非法。"""


def sanitize_path(rel_path: str) -> str:
    """归一化并拒绝穿越。

    上游 origin 是 pin 死的，但如果不挡 ``..``，一个 ``/v1/../../admin`` 之类的路径
    仍可能被上游解释成别的资源。配合可被 CSRF 改写的 base_url（见准入项 A2），
    这个反代就会变成一个"给任意请求附加付费 key"的通用代理。
    """
    p = C.normalize_v1_path(rel_path)
    if not p:
        return p
    segments = p.split("/")
    if any(seg in {".", ".."} for seg in segments):
        raise PathRejected(f"路径含穿越片段：{rel_path!r}")
    if "://" in p or p.startswith("//"):
        raise PathRejected(f"路径不能是绝对 URL：{rel_path!r}")
    return p


def forward_headers(incoming: Mapping[str, str], api_key: str) -> dict[str, str]:
    """构造转发给上游的请求头。

    剥掉的是**全部**客户端 IP 类头，不只是 ``X-Forwarded-For``。只剥 XFF 的话，
    ``Forwarded`` 与 ``CF-Connecting-IP`` 会把真实来源交给上游——中转形同白建。

    注意这与"客户端 → 星槎"那一跳相反：那一跳恰恰**需要** XFF 才能记录真实来源 IP。
    两处不能照抄同一条配置。
    """
    out = {k: v for k, v in incoming.items() if k.lower() not in C.STRIP_REQUEST_HEADERS}
    out["authorization"] = f"Bearer {api_key}"
    return out


def response_headers(upstream: Mapping[str, str]) -> dict[str, str]:
    """按**白名单**过滤上游响应头。

    必须是白名单：黑名单式只剥 hop-by-hop 就逐字节透传的话，上游的 ``Set-Cookie``
    会落在你自己的域上（实测上游确实会带），任何 echo/debug 头也一并出去。
    """
    return {k: v for k, v in upstream.items() if k.lower() in C.ALLOW_RESPONSE_HEADERS}


async def read_body_capped(request: Request) -> bytes:
    """读取请求体，超过上限即 413。

    整块缓冲而不是流式转发是有意的：把异步迭代器交给 httpx2 会强制 chunked 编码，
    而部分中转（New API 一类）会拒收 chunked 的请求体。代价是必须自己设上限——
    没有上限时一个大 POST 就能打死这个同时承载全部流量、SQLite 写入和用量缓冲的
    单进程。

    ``Content-Length`` 先查一次是为了**在读之前**就拒掉，不给攻击者免费的带宽；
    但它可以撒谎（或者根本没有，chunked 就没有），所以边读边累计才是真正的防线。
    """
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > C.MAX_BODY_BYTES:
                raise RequestTooLarge
        except ValueError:
            pass  # 头本身畸形，交给下面的累计逻辑

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > C.MAX_BODY_BYTES:
            raise RequestTooLarge
        chunks.append(chunk)
    return b"".join(chunks)


def wants_stream(body: bytes) -> bool:
    """粗判是否流式请求。

    只做一次极轻的探测：这一层的原则是不解析上游语义，判错的代价也只是选错了
    转发方式（两种方式对客户端等价），所以不值得为它引入完整的 JSON 解析开销。
    """
    return b'"stream"' in body and b'"stream": false' not in body and b'"stream":false' not in body


@router.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"],
    include_in_schema=False,
)
async def passthrough(path: str, request: Request) -> Response:
    """把请求原样转发给上游。

    这条路由**必须最后注册**：它是 catch-all，先注册会把自有路径也吞掉。
    """
    state = request.app.state.xc

    try:
        rel = sanitize_path(path)
    except PathRejected as e:
        log.warning("拒绝路径：%s", e)
        raise UpstreamError(400, log_detail=str(e)) from e

    pool = state.upstream
    if not pool.configured:
        raise UpstreamNotConfigured
    cfg = pool.config
    client = pool.client()

    body = await read_body_capped(request)
    headers = forward_headers(request.headers, cfg.api_key)
    url = f"{cfg.normalized_base()}/{rel}"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    # 直通层的配额**默认不执行**（契约 §3.9 冻结了这一点，打开它是一次收紧）。
    # 打开之后 /version 的 features 会多一项，调用方能探测到这个变化。
    reservation = None
    if state.quota is not None and state.settings.quota_on_passthrough:
        principal = getattr(request.state, "principal", None)
        reservation = state.quota.reserve(
            user_id=principal.user_id if principal else 1,
            token_id=principal.token_id if principal else None,
            agent_id=None,
        )

    tracker = RunTracker(request, kind="passthrough", model=_model_hint(body, rel))
    if reservation is not None:
        tracker.attach_reservation(reservation)
    return await execute_forward(
        client, request.method, url, headers, body, tracker, state.settings.request_timeout
    )


def _model_hint(body: bytes, rel: str) -> str:
    """从请求体里粗取 model，取不到就用路径。

    直通层不解析上游语义，但 run 记录里没有 model 就几乎没法看——所以这里做一次
    极轻的探测，失败就退回路径名，不为此引入完整解析。
    """
    import json

    try:
        payload = json.loads(body)
        if isinstance(payload, dict) and isinstance(payload.get("model"), str):
            return payload["model"]
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        pass
    return f"({rel})"


async def execute_forward(
    client: httpx2.AsyncClient,
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
    tracker: RunTracker,
    timeout: float,
) -> Response:
    """执行转发并记录。Agent 路径与直通路径共用这一条。

    流式与非流式的**提交时机不同**：非流式在这里就提交；流式交给
    ``tracker.wrap_stream``，因为用量在最后一帧里，此刻流还没开始发。
    """
    try:
        if wants_stream(body):
            resp = await forward_streaming(client, method, url, headers, body, tracker)
            tracker.finish_ok()
            return resp
        resp = await forward_buffered(client, method, url, headers, body)
        tracker.finish_ok()
        tracker.absorb_body(bytes(resp.body))
        await tracker.submit()
        return resp
    except XingchaError as e:
        tracker.finish_error(e.error_type.value, C.RunStatus.UPSTREAM_ERROR.value)
        await tracker.submit()
        raise
    except httpx2.TimeoutException as e:
        tracker.finish_error(C.ErrorType.UPSTREAM_TIMEOUT.value, C.RunStatus.TIMEOUT.value)
        await tracker.submit()
        raise UpstreamTimeout(timeout) from e
    except httpx2.RequestError as e:
        # 连接层失败。异常文本可能带完整 URL，所以只进日志、不回显。
        tracker.finish_error(C.ErrorType.UPSTREAM_ERROR.value, C.RunStatus.UPSTREAM_ERROR.value)
        await tracker.submit()
        raise UpstreamError(502, log_detail=f"{type(e).__name__}: {e}") from e


async def forward_buffered(
    client: httpx2.AsyncClient,
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
) -> Response:
    r = await client.request(method, url, headers=headers, content=body)
    return Response(
        content=r.content,
        status_code=r.status_code,
        headers=response_headers(r.headers),
    )


async def forward_streaming(
    client: httpx2.AsyncClient,
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
    tracker: RunTracker | None = None,
) -> Response:
    """流式转发。

    用 ``aiter_raw()`` 而不是 ``aiter_bytes()``：前者给的是**未解码**的字节，配合原样
    转发的 ``content-encoding`` 才自洽。用 ``aiter_bytes()`` 会解压，但响应头里仍写着
    ``gzip``，客户端会二次解压失败。

    ``client.stream()`` 的上下文必须活到整个响应体发送完毕，所以这里在生成器**内部**
    打开它，而不是先拿到 response 再返回——后者会在返回时关掉连接，客户端只收到空流。
    """
    # 用一个 Future 把状态码与响应头从生成器里传出来，因为 StreamingResponse 需要
    # 在开始迭代之前就知道它们。
    head: asyncio.Future[tuple[int, dict[str, str]]] = asyncio.get_running_loop().create_future()

    async def body_iter() -> AsyncIterator[bytes]:
        try:
            async with client.stream(method, url, headers=headers, content=body) as r:
                if not head.done():
                    head.set_result((r.status_code, response_headers(r.headers)))
                async for chunk in r.aiter_raw():
                    yield chunk
        except Exception as e:
            if not head.done():
                head.set_exception(e)
                return
            # 流已经开始了，没法再改状态码。记日志并静默结束——客户端会看到一个
            # 未以 [DONE] 结尾的流，这是 SSE 下唯一诚实的失败方式。
            log.warning("流式转发中断：%s: %s", type(e).__name__, e)

    gen = body_iter()
    # 先驱动到拿到响应头为止
    first: bytes | None = None
    try:
        first = await anext(gen)
    except StopAsyncIteration:
        first = None

    if head.done() and head.exception() is not None:
        raise head.exception()  # type: ignore[misc]
    status, hdrs = await head

    async def full() -> AsyncIterator[bytes]:
        if first is not None:
            yield first
        async for chunk in gen:
            yield chunk

    stream: AsyncIterator[bytes] = full()
    if tracker is not None:
        # 流式的用量在最后一帧里，所以记录要等流耗尽才提交
        stream = tracker.wrap_stream(stream)
    return StreamingResponse(stream, status_code=status, headers=hdrs)
