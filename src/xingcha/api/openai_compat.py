"""OpenAI 兼容层：``/v1/models`` 与 ``/v1/chat/completions``。

``GET /v1/models`` 是卖点丙的全部——Agent 以 slug 出现在客户端的模型下拉框里。
OpenRouter 的 Presets 可以被调用，但实测**不出现在**模型列表里，这是关键区别。

**注册顺序有意义**：自有路径必须在 catch-all 直通之前注册，否则会被吞掉。
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse

from .. import contract as C
from ..contract import ModelKind, ModelRefInvalid, classify_model
from ..core.builder import BuildOptions
from ..core.upstream import UpstreamNotConfigured
from ..errors import (
    ModelInvalid,
    ModelNotFound,
    ParamUnsupported,
    StreamUnsupported,
    UpstreamError,
    XingchaError,
)
from ..services import agent as agent_svc
from ..services import run as run_svc
from .passthrough import execute_forward, forward_headers, read_body_capped
from .runlog_mw import RunTracker
from .sse import SameTaskEventStream

log = logging.getLogger(__name__)

router = APIRouter()

#: OPTIONS 单独一个 router：预检请求不带 Authorization，必须免鉴权。
options_router = APIRouter()


# =============================================================================
# GET /v1/models
# =============================================================================


def _upstream_row(info: Any) -> dict[str, Any]:
    return {
        "id": info.id,
        "object": "model",
        "created": info.created or 0,
        "owned_by": C.OWNED_BY_UPSTREAM,
        C.EXT_KEY: {
            "v": C.EXT_SHAPE_VERSION,
            "kind": "model",
            "name": info.name,
            "native_schema": info.supports_native_schema,
        },
    }


async def _build_model_list(request: Request, owned_by: str | None) -> dict[str, Any]:
    state = request.app.state.xc
    rows: list[dict[str, Any]] = []

    # --- Agent 行在前 ---
    # 顺序必须冻结：部分客户端取 data[0] 当默认模型，换排序即静默换模型。
    agent_rows: list[dict[str, Any]] = []
    if owned_by in (None, C.OWNED_BY_XINGCHA):
        async with state.sessionmaker() as session:
            agent_rows = [
                agent_svc.agent_row_for_models_api(a) for a in await agent_svc.list_active(session)
            ]
        rows.extend(agent_rows)

    # --- 上游行在后 ---
    stale = False
    if owned_by in (None, C.OWNED_BY_UPSTREAM) and state.settings.models_include_upstream:
        catalog = state.catalog
        if state.upstream.configured:
            await catalog.ensure_fresh(state.upstream.client(), state.upstream.config.api_key)
        stale = catalog.is_stale or catalog.is_empty
        seen = {r["id"] for r in agent_rows}
        for info in catalog.all():
            # 按 id 去重且 Agent 优先：同名时 Agent 赢
            if info.id in seen:
                continue
            rows.append(_upstream_row(info))

    body: dict[str, Any] = {"object": "list", "data": rows}
    if stale:
        # stale-while-error：返回上次成功的快照并**明说**它是旧的。
        # 静默少返回会把客户端会话配置里的上游模型抹掉；直接 502 会让客户端判定
        # 整个端点不可用，连 Agent 也用不了。
        body[C.EXT_KEY] = {
            "v": C.EXT_SHAPE_VERSION,
            "catalog_stale": True,
            "fetched_at": state.catalog.fetched_at,
            "reason": state.catalog.last_error,
        }
    return body


@router.get("/models", include_in_schema=False)
async def list_models(
    request: Request,
    owned_by: str | None = Query(default=None),
) -> dict[str, Any]:
    if owned_by is not None and owned_by not in C.OWNED_BY_VALUES:
        raise ModelInvalid(f"owned_by 只能是 {sorted(C.OWNED_BY_VALUES)} 之一，收到 {owned_by!r}")
    return await _build_model_list(request, owned_by)


@router.get("/models/{model_id}", include_in_schema=False)
async def retrieve_model(model_id: str, request: Request) -> dict[str, Any]:
    """OpenAI 标准的 retrieve-model。

    **这个端点必须由星槎自己实现。** Cherry Studio、Continue 一类客户端会用它验证
    模型是否存在；如果归给反代，客户端拿 Agent slug 来问就会打到 OpenRouter 拿回
    上游 404，据此判定「这个模型不存在」。而按演进规则事后从反代收回它算破坏性变更
    ——那就等于这个端点永久坏掉。

    只处理**单段** id：Agent slug 永不含 ``/``，而上游 model id 一定含 ``/``，
    多段的情形（含 OpenRouter 自己的 ``/models/{author}/{slug}/endpoints``）
    由 catch-all 直通处理，路由层已按段数分开。
    """
    body = await _build_model_list(request, None)
    for row in body["data"]:
        if row["id"] == model_id:
            return row
    raise ModelNotFound(model_id)


# =============================================================================
# POST /v1/chat/completions
# =============================================================================


def _reject_unsupported_fields(payload: dict[str, Any]) -> None:
    """三态里的 reject 表。

    拒绝而不是静默忽略：这些字段会绕过服务端的运行护栏（``retries`` / ``usage_limits``
    能覆盖 Agent 构造时的值，``response_format`` 能覆盖输出形状），静默忽略会让调用方
    以为自己设置生效了。
    """
    for field in C.REQUEST_REJECT:
        if field in payload and payload[field] is not None:
            raise ParamUnsupported(field)


@router.post("/chat/completions", include_in_schema=False)
async def chat_completions(request: Request) -> Response:
    """按 ``model`` 字段分派。

    - 含 ``/`` → 上游裸模型，透明转发（**v1 的核心价值**）
    - 不含 ``/`` → Agent slug；M1 还没有 Agent 面，一律 404

    404 而不是"猜测性地当上游模型转发"：那样一个拼错的 slug 会静默变成一次真实的
    付费调用，而调用方以为自己在调 Agent。
    """
    state = request.app.state.xc
    body = await read_body_capped(request)

    try:
        payload = json.loads(body) if body else {}
    except json.JSONDecodeError as e:
        raise ModelInvalid(f"请求体不是合法 JSON：{e.msg}") from e
    if not isinstance(payload, dict):
        raise ModelInvalid("请求体必须是一个 JSON 对象")

    _reject_unsupported_fields(payload)

    model = payload.get("model")
    if not isinstance(model, str) or not model:
        raise ModelInvalid("缺少 model 字段")

    try:
        ref = classify_model(model)
    except ModelRefInvalid as e:
        raise ModelInvalid(str(e)) from e

    if ref.kind is ModelKind.AGENT:
        return await _run_agent(request, ref.value, payload, model)

    # 上游裸模型：透明转发。
    if not state.upstream.configured:
        raise UpstreamNotConfigured
    cfg = state.upstream.config
    client = state.upstream.client()

    # 显式命名空间 xc:model/<id> 要还原成上游认识的 id 再转发
    if ref.explicit:
        payload["model"] = ref.value
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    headers = forward_headers(request.headers, cfg.api_key)
    url = f"{cfg.normalized_base()}/chat/completions"

    tracker = RunTracker(request, kind="passthrough", model=ref.value)
    return await execute_forward(
        client, "POST", url, headers, body, tracker, state.settings.request_timeout
    )


# =============================================================================
# OPTIONS
# =============================================================================


@options_router.options("/{path:path}", include_in_schema=False)
async def options_handler(path: str, request: Request) -> Response:
    """``/v1`` 下任何路径的 OPTIONS 一律由星槎应答，永不反代。

    不这么做的话，浏览器客户端（Open WebUI、自建前端）直连时的 CORS 预检会由
    OpenRouter 的策略决定，而星槎自己的响应又不带 CORS 头——表现为"非流式偶尔能用、
    浏览器直连必挂"。而等到要支持浏览器客户端时再拦截 OPTIONS，按演进规则算
    破坏性变更。

    预检请求**不带** Authorization（浏览器规定如此），所以这条路由必须免鉴权。
    """
    origins = request.app.state.xc.settings.cors_origin_list
    origin = request.headers.get("origin")
    headers = {"Allow": "GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS"}
    # 默认空 = 不发任何 CORS 头。放开 origin 是纯加法，所以默认可以最严。
    if origin and origin in origins:
        headers |= {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": headers["Allow"],
            "Access-Control-Allow-Headers": "authorization, content-type",
            "Access-Control-Max-Age": "600",
            "Vary": "Origin",
        }
    return Response(status_code=204, headers=headers)


# =============================================================================
# Agent 调用
# =============================================================================


async def _run_agent(
    request: Request, slug: str, payload: dict[str, Any], requested_model: str
) -> Response:
    """走 Agent：解析 → 取运行时 → 执行 → 转响应。

    结构化 Agent 对 ``stream=true`` **明确返回 400**，而不是流一半 JSON 让客户端
    解析失败。诚实报错优于假装支持。
    """
    state = request.app.state.xc
    if state.provider is None:
        raise UpstreamNotConfigured

    async with state.sessionmaker() as session:
        resolved = await agent_svc.resolve(session, slug)

    wants_streaming = bool(payload.get("stream"))
    if wants_streaming and resolved.is_structured:
        raise StreamUnsupported(requested_model)

    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ModelInvalid("messages 必须是一个非空数组")
    prompt, extra_instructions = run_svc.to_prompt(messages)

    # 配额：**检查并占掉名额**，就在最贵的那一步之前。
    #
    # 放在这里而不是函数开头，是为了让 stream_unsupported / messages 格式错这类
    # 早期拒绝不白吃名额——它们根本没打到上游。
    #
    # 占用与检查在同一个同步块里，所以并发请求不可能全部通过检查再一起计数。
    principal = getattr(request.state, "principal", None)
    reservation = None
    if state.quota is not None:
        reservation = state.quota.reserve(
            user_id=principal.user_id if principal else 1,
            token_id=principal.token_id if principal else None,
            agent_id=resolved.agent_id,
        )

    rt = await run_svc.get_runtime(
        resolved,
        cache=state.runtimes,
        provider=state.provider,
        options=BuildOptions(),
        concurrency=state.concurrency,
    )

    tracker = RunTracker(request, kind="agent", model=requested_model)
    if reservation is not None:
        tracker.attach_reservation(reservation)
    tracker.rec.agent_id = resolved.agent_id
    tracker.rec.agent_version = resolved.version
    tracker.rec.tier = resolved.tier.value

    async def fail(e: XingchaError) -> None:
        tracker.finish_error(e.error_type.value, _status_for(e))
        # 失败也要落用量：一次重试耗尽的调用照样花了钱，不记就等于账单少报。
        tracker.rec.schema_violations = rt.counters.violations
        tracker.rec.schema_retries = rt.counters.retries
        await tracker.submit()

    if wants_streaming:
        return await _stream_agent(request, rt, tracker, prompt, extra_instructions, fail)

    try:
        outcome = await run_svc.execute(
            rt,
            prompt=prompt,
            extra_instructions=extra_instructions,
            run_timeout=state.settings.run_timeout,
        )
    except XingchaError as e:
        await fail(e)
        raise

    _absorb(tracker, outcome, state.catalog, state.cost_sink)
    tracker.finish_ok()
    await tracker.submit()
    return JSONResponse(run_svc.to_openai_response(outcome, model=requested_model))


async def _stream_agent(
    request: Request,
    rt: Any,
    tracker: RunTracker,
    prompt: str,
    extra_instructions: str | None,
    fail: Callable[[XingchaError], Awaitable[None]],
) -> Response:
    """真流式（纯文本 Agent）。

    **第一帧要在构造 StreamingResponse 之前拉出来。** ``run_stream()`` 的进入动作
    会真的发出上游请求，所以这一拉能把连不上、401、模型不存在这类故障变成一个
    正常的 502/40x JSON；等 ``StreamingResponse`` 一构造，200 就提交了，之后所有
    失败只能表达成"流没有以 [DONE] 结尾"。
    """
    state = request.app.state.xc

    async def settle(outcome: run_svc.RunOutcome, aborted: Exception | None) -> None:
        _absorb(tracker, outcome, state.catalog, state.cost_sink)
        if aborted is None:
            tracker.finish_ok()
        else:
            tracker.finish_error(C.ErrorType.UPSTREAM_ERROR.value, C.RunStatus.UPSTREAM_ERROR.value)
        await tracker.submit()

    gen = run_svc.stream_frames(
        rt,
        prompt=prompt,
        extra_instructions=extra_instructions,
        run_timeout=state.settings.run_timeout,
        model=tracker.rec.model,
        run_id=tracker.rec.id,
        debounce=state.settings.stream_debounce_seconds,
        on_outcome=settle,
    )

    try:
        first = await anext(gen)
    except StopAsyncIteration:  # pragma: no cover - 生成器至少发一帧
        await gen.aclose()
        raise UpstreamError(502, log_detail="流式生成器一帧未发") from None
    except XingchaError as e:
        await gen.aclose()
        await fail(e)
        raise

    async def body() -> AsyncIterator[bytes]:
        yield first.encode("utf-8")
        async for frame in gen:
            yield frame.encode("utf-8")

    # 不是 StreamingResponse：它在子任务里迭代，而并发闸按任务记账。见 api/sse.py。
    return SameTaskEventStream(body(), request=request)


def _status_for(e: XingchaError) -> str:
    from ..contract import ErrorType, RunStatus

    return {
        ErrorType.SCHEMA_VIOLATION: RunStatus.SCHEMA_FAILED.value,
        ErrorType.REQUEST_TIMEOUT: RunStatus.TIMEOUT.value,
        ErrorType.UPSTREAM_TIMEOUT: RunStatus.TIMEOUT.value,
        ErrorType.QUOTA_EXCEEDED: RunStatus.QUOTA.value,
    }.get(e.error_type, RunStatus.UPSTREAM_ERROR.value)


def _absorb(
    tracker: RunTracker, outcome: run_svc.RunOutcome, catalog: Any, cost_sink: Any = None
) -> None:
    """把运行结果写进 run 记录，并结算费用。

    费用优先用**上游自己报的**（经 CostSink），拿不到才回落目录价。
    上游那个是唯一非预估的数字，而 pydantic-ai 填的 usage.cost 是估价——
    实测两者能差 400 倍。
    """
    import json as _json

    from .runlog_mw import price

    rec = tracker.rec
    rec.usage_model = outcome.model_id
    rec.input_tokens = outcome.input_tokens
    rec.output_tokens = outcome.output_tokens
    rec.cache_read_tokens = outcome.cache_read_tokens
    rec.requests = outcome.requests
    rec.tool_calls = outcome.tool_calls
    rec.schema_violations = outcome.schema_violations
    rec.schema_retries = outcome.schema_retries
    rec.tier = outcome.tier.value
    if outcome.extra:
        rec.extra_json = _json.dumps(outcome.extra, ensure_ascii=False)

    usage = {
        "input_tokens": outcome.input_tokens,
        "output_tokens": outcome.output_tokens,
        "cache_read_tokens": outcome.cache_read_tokens,
    }
    estimate, source = price(catalog, outcome.model_id, usage)

    upstream = (
        cost_sink.take(outcome.response_ids)
        if cost_sink is not None and outcome.response_ids
        else None
    )
    if upstream is not None:
        cost = upstream
        source = C.CostSource.UPSTREAM.value
        if estimate is not None:
            # 记下预估与实际的差，便于观察目录价准不准。不记的话，"预估偏差有多大"
            # 这个问题永远只能靠人工对账回答。
            extra = _json.loads(rec.extra_json) if rec.extra_json else {}
            extra["cost_estimate"] = str(estimate)
            extra["cost_delta"] = str(upstream - estimate)
            rec.extra_json = _json.dumps(extra, ensure_ascii=False)
    else:
        cost = estimate

    rec.cost_usd = cost
    rec.cost_source = source
    outcome.cost_usd = cost
    outcome.cost_source = source
