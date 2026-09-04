"""按令牌的速率与并发限制。

**必须同时作用于 Agent 路径与直通路径。** 直通路径绕开了配额（v1 不做配额），
如果连速率限制也没有，那它就是一个不计量、不限并发的付费 key 放大器：一把泄漏的
key 能按线速抽干余额。

进程内内存实现，不落库——这依赖单 worker（契约 §9 的 ``REQUIRED_WORKERS``）。
多 worker 下每个进程各有一份计数，限流会变成 N 倍，这是启动时断言单 worker 的
理由之一。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field

from ..errors import QuotaExceeded


@dataclass
class _Bucket:
    """一个令牌的窗口计数与在飞计数。"""

    hits: deque[float] = field(default_factory=deque)
    inflight: int = 0


class RateLimiter:
    """滑动窗口 + 在飞并发。

    用滑动窗口而不是固定窗口：固定窗口在窗口边界处允许两倍突发（窗口末尾打满、
    下一窗口开头再打满），对一个按 token 计费的上游来说那是真金白银。
    """

    def __init__(self, *, per_minute: int, concurrent: int) -> None:
        self._per_minute = per_minute
        self._concurrent = concurrent
        self._buckets: dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, key: str) -> None:
        """占一个名额。超限抛 :class:`QuotaExceeded`（429）。

        调用方必须在 ``finally`` 里 :meth:`release`，否则在飞计数会泄漏——
        泄漏的表现是这个 token 越用越慢直到完全被拒，而且重启才能恢复。
        """
        now = time.monotonic()
        async with self._lock:
            b = self._buckets.setdefault(key, _Bucket())
            cutoff = now - 60.0
            while b.hits and b.hits[0] < cutoff:
                b.hits.popleft()

            if b.inflight >= self._concurrent:
                raise QuotaExceeded("token", "concurrent", "requests")
            if len(b.hits) >= self._per_minute:
                raise QuotaExceeded("token", "minute", "requests")

            b.hits.append(now)
            b.inflight += 1

    async def release(self, key: str) -> None:
        async with self._lock:
            b = self._buckets.get(key)
            if b is not None and b.inflight > 0:
                b.inflight -= 1

    async def prune(self, *, max_idle_seconds: float = 3600.0) -> None:
        """清掉长期不活跃的桶，避免字典无限增长。

        只在没有在飞请求时才清——否则会把 inflight 计数一起丢掉，导致并发上限失效。
        """
        cutoff = time.monotonic() - max_idle_seconds
        async with self._lock:
            for k in [
                k
                for k, b in self._buckets.items()
                if b.inflight == 0 and (not b.hits or b.hits[-1] < cutoff)
            ]:
                del self._buckets[k]

    def snapshot(self, key: str) -> tuple[int, int]:
        """``(最近一分钟的请求数, 在飞数)``。供 /readyz 与管理面展示。"""
        b = self._buckets.get(key)
        if b is None:
            return 0, 0
        cutoff = time.monotonic() - 60.0
        return sum(1 for t in b.hits if t >= cutoff), b.inflight


class _Guard:
    def __init__(self, limiter: RateLimiter, key: str) -> None:
        self._limiter = limiter
        self._key = key

    async def __aenter__(self) -> None:
        await self._limiter.acquire(self._key)

    async def __aexit__(self, *_exc: object) -> None:
        await self._limiter.release(self._key)


def guard(limiter: RateLimiter, key: str) -> _Guard:
    """``async with guard(limiter, kid): ...``

    包装成上下文管理器是为了让"忘记 release"这件事不可能发生——在飞计数泄漏的表现是
    某个 token 越用越慢直到完全被拒，且只有重启能恢复，很难联想到根因。
    """
    return _Guard(limiter, key)
