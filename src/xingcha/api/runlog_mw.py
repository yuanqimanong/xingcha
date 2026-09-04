"""把一次 ``/v1`` 调用记成一行 run。

Agent 路径与直通路径**共用**这条链路：两条路径花的是同一把上游 key 的钱，分开记
会让「这个月一共花了多少」变成一次 UNION，而那正是最常被问的问题。

流式的用量在最后一帧里。为了不丢掉流式调用的费用，这里给流套一层很薄的嗅探——
只看尾部，不缓冲整个流（缓冲整个流就等于取消了流式）。
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

from .. import contract as C
from ..core.models_catalog import ModelInfo, ModelsCatalog
from ..services.runlog import RunRecord

log = logging.getLogger(__name__)

#: 流式响应里用来找 usage 的尾部窗口。usage 帧总在最后几帧，没必要留全部。
_TAIL_BYTES = 64 * 1024


def extract_usage(payload: dict[str, Any]) -> dict[str, int]:
    """从 OpenAI 形状的响应里取 token 明细。

    上游随时可能加维度，所以这里只认已知字段、其余交给 ``extra_json``——
    一个因为多了个键就抛异常的解析器会让计量在某天早上突然全线失败。
    """
    u = payload.get("usage") or {}
    if not isinstance(u, dict):
        return {}
    details = u.get("prompt_tokens_details") or {}
    if not isinstance(details, dict):
        details = {}
    return {
        "input_tokens": int(u.get("prompt_tokens") or 0),
        "output_tokens": int(u.get("completion_tokens") or 0),
        "cache_read_tokens": int(details.get("cached_tokens") or 0),
    }


def extract_upstream_cost(payload: dict[str, Any]) -> Decimal | None:
    """取上游**自己报的**费用。

    OpenRouter 会在 ``usage.cost`` 里回真实扣费（还有
    ``usage.cost_details.upstream_inference_cost``）。这是唯一非预估的数字。

    为什么必须自己取：pydantic-ai 会自动填 ``RunUsage.cost``，但填的是
    **genai-prices 的估价**，不是上游账单——上游 body 里那个 ``cost`` 因为是 float
    被 ``_map_usage`` 的 ``isinstance(v, int)`` 过滤掉了，哪儿都没留（实测）。
    所以"预估 vs 实际"这件事只能在 HTTP 层做。
    """
    u = payload.get("usage")
    if not isinstance(u, dict):
        return None
    for key in ("cost", "total_cost"):
        raw = u.get(key)
        if isinstance(raw, int | float | str):
            try:
                value = Decimal(str(raw))
            except Exception:
                continue
            # 上游对免费模型会回 0，那是真实的 0 而不是"没有数据"——保留它。
            if value >= 0:
                return value
    return None


def price(
    catalog: ModelsCatalog, model_id: str, usage: dict[str, int]
) -> tuple[Decimal | None, str]:
    """按目录价算费用，返回 ``(金额, 来源)``。

    catalog 是主价源：实测它对在售模型 424/424 全有价格，而 genai-prices 只覆盖
    66.7%（漏的全是新模型与 ``:free`` / ``:batch`` 变体，恰好是最省钱那些）。

    ``cache_read_tokens`` 是 ``input_tokens`` 的**子集**（包含式语义），所以要先减
    再按缓存单价补回来。不做这一步会系统性高估——实测同一次调用高估约 16%。
    """
    info: ModelInfo | None = catalog.get(model_id)
    if info is None or info.prompt_price is None or info.completion_price is None:
        return None, C.CostSource.UNKNOWN.value

    inp = usage.get("input_tokens", 0)
    out = usage.get("output_tokens", 0)
    cached = min(usage.get("cache_read_tokens", 0), inp)

    total = (inp - cached) * info.prompt_price + out * info.completion_price
    if cached:
        # 缓存读单价缺失时保守按原价计——宁可报高一点，也不要悄悄少报
        total += cached * (info.cache_read_price or info.prompt_price)
    return total, C.CostSource.CATALOG.value


def fill_from_body(rec: RunRecord, body: bytes, catalog: ModelsCatalog) -> None:
    """从非流式响应体里补齐用量与费用。解析失败就留空，不影响主调用。"""
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return
    if not isinstance(payload, dict):
        return
    _apply(rec, payload, catalog)


def _apply(rec: RunRecord, payload: dict[str, Any], catalog: ModelsCatalog) -> None:
    usage = extract_usage(payload)
    if not usage:
        return
    rec.input_tokens = usage.get("input_tokens", 0)
    rec.output_tokens = usage.get("output_tokens", 0)
    rec.cache_read_tokens = usage.get("cache_read_tokens", 0)
    rec.requests = 1
    # 上游回显的 model 才是真正执行的那个（可能被路由到变体）
    executed_model = payload.get("model") or rec.model
    rec.usage_model = executed_model

    # **上游自己报的费用优先。** 那是唯一非预估的数字。
    #
    # 目录价是好的回落，但它算的是"按标价应该花多少"，而上游实际扣费会受平台抽成、
    # 缓存计价、provider 路由影响。两者有系统性差异，而 cost_source 让这件事
    # 在数据里是可见的——不至于让人把预估当账单。
    upstream = extract_upstream_cost(payload)
    if upstream is not None:
        rec.cost_usd = upstream
        rec.cost_source = C.CostSource.UPSTREAM.value
        # 顺手记下预估与实际的差，便于观察目录价准不准
        estimate, _ = price(catalog, executed_model, usage)
        if estimate is not None:
            rec.extra_json = _merge_extra(
                rec.extra_json,
                {"cost_estimate": str(estimate), "cost_delta": str(upstream - estimate)},
            )
        return

    cost, source = price(catalog, executed_model, usage)
    rec.cost_usd = cost
    rec.cost_source = source


def _merge_extra(existing: str | None, extra: dict[str, Any]) -> str:
    """往 ``extra_json`` 里加字段而不覆盖已有的。"""
    base: dict[str, Any] = {}
    if existing:
        try:
            loaded = json.loads(existing)
            if isinstance(loaded, dict):
                base = loaded
        except json.JSONDecodeError:
            pass
    base.update(extra)
    return json.dumps(base, ensure_ascii=False)


def sniff_stream(
    inner: AsyncIterator[bytes], rec: RunRecord, catalog: ModelsCatalog
) -> AsyncIterator[bytes]:
    """透传流，同时留一个尾部窗口用来找 usage 帧。

    只留尾部：把整个流缓冲下来就等于取消了流式，而 usage 总在最后几帧。
    """

    async def gen() -> AsyncIterator[bytes]:
        tail = bytearray()
        try:
            async for chunk in inner:
                tail.extend(chunk)
                if len(tail) > _TAIL_BYTES:
                    del tail[: len(tail) - _TAIL_BYTES]
                yield chunk
        finally:
            _scan_sse_tail(bytes(tail), rec, catalog)

    return gen()


def _scan_sse_tail(tail: bytes, rec: RunRecord, catalog: ModelsCatalog) -> None:
    """在 SSE 尾部倒着找第一个带 usage 的帧。"""
    try:
        text = tail.decode("utf-8", errors="ignore")
    except Exception:  # pragma: no cover
        return
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("usage"):
            _apply(rec, payload, catalog)
            return


class RunTracker:
    """一次调用的记录器。Agent 路径与直通路径共用。

    **提交时机对流式和非流式不同**，这是这个类存在的理由：非流式在处理器返回时
    用量就齐了；流式的用量在最后一帧里，处理器返回时流还没开始发。所以流式必须
    等 :meth:`wrap_stream` 的迭代器耗尽才提交——放在 ``finally`` 里提交会得到一条
    永远 0 token 的记录。
    """

    __slots__ = ("_buffer", "_catalog", "_reservation", "_submitted", "_t0", "rec")

    def __init__(self, request: Any, *, kind: str, model: str) -> None:
        import time

        from ..services.runlog import RunRecord, new_run_id

        state = request.app.state.xc
        principal = getattr(request.state, "principal", None)
        self.rec = RunRecord(
            id=new_run_id(),
            kind=kind,
            model=model,
            user_id=principal.user_id if principal else 1,
            token_id=principal.token_id if principal else None,
        )
        self._buffer = state.usage
        self._catalog = state.catalog
        #: 配额名额在**检查时**就占掉了（见 services/quota.py 的难点二），
        #: 这里只负责补上实际费用。
        self._reservation: Any = None
        self._t0 = time.monotonic()
        self._submitted = False
        # run_id 要能回到 5xx 的响应体里——那是排障时唯一的抓手
        request.state.run_id = self.rec.id

    def attach_reservation(self, reservation: Any) -> None:
        """把配额预留交给 tracker，由它在提交时结算。"""
        self._reservation = reservation

    def release_reservation(self) -> None:
        """调用没有发生（早期拒绝），把名额还回去。

        不释放的话，一次 stream_unsupported 之类的拒绝也会白吃一个名额，
        而那类拒绝根本没打到上游、没花钱。
        """
        if self._reservation is not None:
            self._reservation.release()

    def finish_ok(self) -> None:
        self.rec.status = C.RunStatus.OK.value

    def finish_error(self, error_type: str, status: str) -> None:
        self.rec.error_type = error_type
        self.rec.status = status

    def absorb_body(self, body: bytes) -> None:
        fill_from_body(self.rec, body, self._catalog)

    def wrap_stream(self, inner: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        """套一层尾部嗅探，并在流结束时提交记录。"""
        sniffed = sniff_stream(inner, self.rec, self._catalog)

        async def gen() -> AsyncIterator[bytes]:
            try:
                async for chunk in sniffed:
                    yield chunk
            finally:
                await self.submit()

        return gen()

    async def submit(self) -> None:
        """提交记录。重复调用是安全的——流式路径上很容易走到两次。"""
        import time

        if self._submitted or self._buffer is None:
            return
        self._submitted = True
        from ..db.models import utcnow

        self.rec.latency_ms = int((time.monotonic() - self._t0) * 1000)
        self.rec.finished_at = utcnow()

        # 补上实际费用。次数在预留时已经算过——不能再加一次，否则限额会减半。
        #
        # **无论成败都结算**：一次重试耗尽的调用照样花了钱（1+retries 次模型调用），
        # 不结算等于让失败的调用免费，而那恰好是最贵的一类。
        if self._reservation is not None:
            self._reservation.settle(self.rec.cost_usd)

        await self._buffer.add(self.rec)
