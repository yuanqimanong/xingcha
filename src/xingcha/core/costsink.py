"""上游真实费用的收集点。

------------------------------------------------------------------------------
为什么需要这个东西
------------------------------------------------------------------------------

pydantic-ai 会自动填 ``RunUsage.cost``，但填的是 **genai-prices 的估价**，不是上游
账单。实测：上游 body 里带 ``usage.cost = 0.0271``，而 ``result.usage.cost`` 是
``0.0000625``——两个数差了 400 倍，而且上游那个真实值**哪儿都没留**（它是 float，
被 ``_map_usage`` 的 ``isinstance(v, int)`` 过滤掉了）。

所以"预估 vs 实际"这件事只能在 HTTP 层做：挂一个 httpx2 的 event hook，把上游报的
费用按响应 id 记下来，运行结束后用 ``provider_response_id`` 取回。

``provider_response_id`` 是 pydantic-ai 暴露的（实测可取），所以关联是可靠的，
不需要靠时间戳或调用顺序去猜。

------------------------------------------------------------------------------
为什么是有界的
------------------------------------------------------------------------------

这是一个"写进来等着被取走"的缓存，而取走的那一步可能不发生（请求超时、进程被 kill）。
无界的话它会慢慢吃掉内存，而这台机器只有 1GB。所以用 LRU 有界 + 取走即删。
"""

from __future__ import annotations

import json
import logging
from collections import OrderedDict
from decimal import Decimal
from typing import Any

import httpx2

log = logging.getLogger(__name__)


class CostSink:
    """按上游响应 id 暂存真实费用。

    一次运行可能有多次上游调用（schema 重试、工具往返、两阶段），所以同一个
    ``run`` 会有多个响应 id。累加时用的是**最后一次**响应的 id——而 pydantic-ai 的
    ``RunUsage`` 本身也是整轮累计，所以口径要对上，只能在这里也累加。
    """

    def __init__(self, *, max_entries: int = 512) -> None:
        self._max = max_entries
        self._items: OrderedDict[str, Decimal] = OrderedDict()

    def put(self, response_id: str, cost: Decimal) -> None:
        if not response_id:
            return
        self._items[response_id] = self._items.get(response_id, Decimal(0)) + cost
        self._items.move_to_end(response_id)
        while len(self._items) > self._max:
            self._items.popitem(last=False)

    def take(self, response_ids: list[str]) -> Decimal | None:
        """取走这些响应 id 的费用总和。取走即删。

        返回 None 表示一个都没命中——那说明上游没报费用（不是所有中转都会报），
        此时调用方应当回落到目录价，而不是把费用记成 0。
        """
        total = Decimal(0)
        hit = False
        for rid in response_ids:
            value = self._items.pop(rid, None)
            if value is not None:
                total += value
                hit = True
        return total if hit else None

    def __len__(self) -> int:
        return len(self._items)


def _cost_from_body(body: bytes) -> Decimal | None:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    raw = usage.get("cost")
    if not isinstance(raw, int | float | str):
        return None
    try:
        value = Decimal(str(raw))
    except Exception:
        return None
    return value if value >= 0 else None


def _response_id(body: bytes) -> str:
    try:
        payload = json.loads(body)
        rid = payload.get("id") if isinstance(payload, dict) else None
        return rid if isinstance(rid, str) else ""
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ""


def make_hook(sink: CostSink) -> Any:
    """构造给 ``httpx2.AsyncClient(event_hooks=...)`` 用的响应钩子。

    **绝不能影响主调用。** 这个钩子只做记账，任何异常都吞掉——为了一个"更准的费用
    数字"而让一次成功的模型调用变成 500，是明显不划算的交易。
    """

    async def on_response(response: httpx2.Response) -> None:
        try:
            # 流式响应不能在这里读 body——读了就把流消费掉了，客户端只会收到空流。
            # 流式的费用在最后一帧里，由 runlog_mw 的尾部嗅探负责。
            content_type = response.headers.get("content-type", "")
            if "event-stream" in content_type:
                return
            if response.status_code >= 400:
                return

            body = await response.aread()
            cost = _cost_from_body(body)
            if cost is None:
                return
            rid = _response_id(body)
            if rid:
                sink.put(rid, cost)
        except Exception:
            log.debug("费用钩子失败，忽略", exc_info=True)

    return on_response
