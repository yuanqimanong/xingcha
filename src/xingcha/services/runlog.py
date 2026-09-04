"""调用记录与用量缓冲。

**缓冲必须在进程退出时落盘。** 「批量 flush」+「重启即升级」如果没有关停时的强制
flush，每次升级都会静默丢掉内存里那批 run 行——账单恰好在你最需要它可信的时刻少报，
而且丢了多少无法事后察觉。这是 A9。

缓冲同时有条数与时间双上界：只有时间上界的话，一次突发流量会让内存无界增长；
只有条数上界的话，低流量时最后几条可能几小时都不落盘。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .. import contract as C
from ..db.models import Run, RunUsage, utcnow

log = logging.getLogger(__name__)


@dataclass
class RunRecord:
    """一次调用的完整记录。Agent 路径与直通路径共用。"""

    id: str
    kind: str  # agent | passthrough
    model: str
    user_id: int
    token_id: int | None = None
    agent_id: int | None = None
    agent_version: int | None = None
    tier: str | None = None
    status: str = C.RunStatus.OK.value
    error_type: str | None = None
    latency_ms: int | None = None
    started_at: str = field(default_factory=utcnow)
    finished_at: str | None = None

    # 用量。直通路径从上游响应体里解析，Agent 路径从 RunUsage 拿。
    usage_model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    requests: int = 0
    tool_calls: int = 0
    schema_violations: int = 0
    schema_retries: int = 0
    cost_usd: Decimal | None = None
    cost_source: str = C.CostSource.UNKNOWN.value
    extra_json: str | None = None


def new_run_id() -> str:
    return uuid.uuid4().hex


class UsageBuffer:
    """把 run 行攒起来批量落库，不在请求路径上同步写盘。

    SQLite 的写是串行化的：每次调用都同步写一次，会让写锁成为吞吐瓶颈，而这些行
    对调用方来说并不需要立即可见。
    """

    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        *,
        max_rows: int = 50,
        max_seconds: float = 5.0,
    ) -> None:
        self._maker = maker
        self._max_rows = max_rows
        self._max_seconds = max_seconds
        self._pending: list[RunRecord] = []
        self._lock = asyncio.Lock()
        self._last_flush = time.monotonic()
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    async def add(self, rec: RunRecord) -> None:
        async with self._lock:
            self._pending.append(rec)
            due = (
                len(self._pending) >= self._max_rows
                or time.monotonic() - self._last_flush >= self._max_seconds
            )
        if due:
            await self.flush()

    async def flush(self) -> int:
        """把缓冲里的行写进库。返回写入条数。

        失败时**不丢弃**缓冲——宁可下次重试，也不要因为一次写冲突就永久丢掉账单数据。
        但要防止无界增长：超过阈值时丢最老的，并明确记一条日志。
        """
        async with self._lock:
            batch, self._pending = self._pending, []
            self._last_flush = time.monotonic()
        if not batch:
            return 0

        try:
            async with self._maker() as s:
                for rec in batch:
                    s.add(_to_run(rec))
                    s.add(_to_usage(rec))
                await s.commit()
            return len(batch)
        except Exception:
            log.exception("用量落库失败，%d 条留待下次重试", len(batch))
            async with self._lock:
                self._pending = batch + self._pending
                overflow = len(self._pending) - self._max_rows * 20
                if overflow > 0:
                    del self._pending[:overflow]
                    log.error("用量缓冲溢出，丢弃最老的 %d 条记录", overflow)
            return 0

    def start(self) -> None:
        """启动周期性 flush。低流量时最后几条也能及时落盘。"""
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(self._max_seconds)
                await self.flush()
        except asyncio.CancelledError:
            pass

    async def close(self) -> None:
        """关停时强制落盘。**这是 A9 的实现，不能省。**"""
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        n = await self.flush()
        if n:
            log.info("关停前落盘 %d 条用量记录", n)

    @property
    def pending(self) -> int:
        return len(self._pending)


def _to_run(rec: RunRecord) -> Run:
    return Run(
        id=rec.id,
        kind=rec.kind,
        agent_id=rec.agent_id,
        agent_version=rec.agent_version,
        user_id=rec.user_id,
        token_id=rec.token_id,
        model=rec.model,
        tier=rec.tier,
        status=rec.status,
        error_type=rec.error_type,
        latency_ms=rec.latency_ms,
        started_at=rec.started_at,
        finished_at=rec.finished_at or utcnow(),
    )


def _to_usage(rec: RunRecord) -> RunUsage:
    return RunUsage(
        run_id=rec.id,
        model=rec.usage_model or rec.model,
        input_tokens=rec.input_tokens,
        output_tokens=rec.output_tokens,
        cache_read_tokens=rec.cache_read_tokens,
        cache_write_tokens=rec.cache_write_tokens,
        requests=rec.requests,
        tool_calls=rec.tool_calls,
        schema_violations=rec.schema_violations,
        schema_retries=rec.schema_retries,
        # Decimal 存成 str。float 存不住，而 None（无法定价）必须与真实的 0 费用可区分。
        cost_usd=str(rec.cost_usd) if rec.cost_usd is not None else None,
        cost_source=rec.cost_source,
        extra_json=rec.extra_json,
    )
