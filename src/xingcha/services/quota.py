"""配额执行。

三级主体（user / token / agent）× 三种窗口（day / month / total），可限金额或次数。
三个设计难点：

一、计数在内存里，不从数据库读。用量是异步批量落库的（``UsageBuffer``，5 秒或 50 条
才 flush），去查 ``run_usage`` 求和的话，刚发生的调用还没落盘就会漏算——一次突发能
漏掉整批。所以内存累加器是权威，数据库是持久记录。这依赖单 worker（契约 §9 的
``REQUIRED_WORKERS``）：多 worker 下每个进程各有一份计数，配额会变成 N 倍。启动时从
数据库把当前窗口的已用量读回来播种一次，重启不会让配额归零。

二、检查的同时就预留。``check`` 与 ``record`` 之间隔着一个 ``await``，计数放在调用之
后的话，50 个并发请求会全部通过检查再各自 +1，限额 2 放过 50 个。所以次数在检查时就
占掉（:meth:`QuotaService.reserve`），调用真的没发生时再释放。金额没法预留（调用前不
知道要花多少），天然是事后判定，最多超出"在飞请求数 × 单次费用"——在飞数被 token 级
速率限制封在 8 以内，超出量有界。这一点要如实说，别让人以为金额上限是精确的。

三、窗口口径是 UTC 且自动翻滚。用本地时区会让"今天"的边界随部署机时区变化。翻滚不靠
定时任务（那需要调度器，违反零中间件），而是把窗口标识和已用量存在一起：标识变了就是
进了新窗口，计数自然从零开始。

直通层默认不执行：契约 §8 冻结了 ``PASSTHROUGH_ENFORCES_QUOTA = False``，加闸是收紧。
所以能力做好、默认关，由管理员显式打开并在 ``/version`` 的 ``features`` 里公布。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..db.models import Quota, Run, RunUsage
from ..foundation.errors import QuotaExceeded

log = logging.getLogger(__name__)

SUBJECT_TYPES = ("user", "token", "agent")
WINDOWS = ("day", "month", "total")


def window_key(window: str, *, now: datetime | None = None) -> str:
    """当前窗口的标识（UTC）。标识变了就是进了新窗口；和已用量存在一起，翻滚就不需要
    任何定时任务。
    """
    now = now or datetime.now(UTC)
    if window == "day":
        return now.strftime("%Y-%m-%d")
    if window == "month":
        return now.strftime("%Y-%m")
    if window == "total":
        return "all"
    raise ValueError(f"未知的窗口 {window!r}")


def window_start(window: str, *, now: datetime | None = None) -> datetime | None:
    """窗口的起点，用于从数据库播种。``total`` 返回 None（无起点）。"""
    now = now or datetime.now(UTC)
    if window == "day":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if window == "month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return None


@dataclass(frozen=True, slots=True)
class Rule:
    subject_type: str
    subject_id: int
    window: str
    limit_usd: Decimal | None
    limit_requests: int | None

    @property
    def key(self) -> tuple[str, int, str]:
        return (self.subject_type, self.subject_id, self.window)


@dataclass
class Spent:
    """某个主体在某个窗口里的已用量。``period`` 是窗口标识，和用量存在一起，所以翻滚
    就是"发现标识变了就归零"。
    """

    period: str
    usd: Decimal = Decimal(0)
    requests: int = 0

    def roll_if_needed(self, current: str) -> None:
        if self.period != current:
            self.period = current
            self.usd = Decimal(0)
            self.requests = 0


@dataclass(slots=True)
class Reservation:
    """一次已占用的配额名额。次数在检查时就占掉，所以调用真的没发生时必须释放——否则
    `stream_unsupported` 之类根本没花钱的早期拒绝也会白吃一个名额。
    """

    _service: QuotaService
    _keys: tuple[tuple[str, int, str], ...]
    _released: bool = False
    _settled: bool = False

    def release(self) -> None:
        """调用没有发生，把名额还回去。"""
        if self._released or self._settled:
            return
        self._released = True
        for key in self._keys:
            spent = self._service._spent.get(key)
            if spent is not None and spent.requests > 0:
                spent.requests -= 1

    def settle(self, cost_usd: Decimal | None) -> None:
        """调用发生了，补上实际费用。次数在预留时已经算过。"""
        if self._released:
            return
        self._settled = True
        if cost_usd is None:
            return
        for key in self._keys:
            spent = self._service._spent.get(key)
            if spent is not None:
                spent.usd += cost_usd


class QuotaService:
    """配额的检查与累加。

    内存计数是权威，数据库只在启动时播种。见模块 docstring 的"难点一"。
    """

    def __init__(self, maker: async_sessionmaker[AsyncSession]) -> None:
        self._maker = maker
        self._rules: dict[tuple[str, int, str], Rule] = {}
        self._spent: dict[tuple[str, int, str], Spent] = {}
        self._loaded = False

    # ------------------------------------------------------------ 规则
    async def reload(self) -> int:
        """从数据库重新读规则，并为新出现的规则播种已用量。"""
        async with self._maker() as session:
            rows = (await session.execute(select(Quota))).scalars().all()
            self._rules = {}
            for row in rows:
                rule = Rule(
                    subject_type=row.subject_type,
                    subject_id=row.subject_id,
                    window=row.window,
                    limit_usd=Decimal(row.limit_usd) if row.limit_usd else None,
                    limit_requests=row.limit_requests,
                )
                self._rules[rule.key] = rule

            for key in self._rules:
                if key not in self._spent:
                    self._spent[key] = await self._seed(session, key)

        # 规则被删掉时把对应的计数也丢掉，避免字典无限增长
        for key in list(self._spent):
            if key not in self._rules:
                del self._spent[key]

        self._loaded = True
        if self._rules:
            log.info("已加载 %d 条配额规则", len(self._rules))
        return len(self._rules)

    async def _seed(self, session: AsyncSession, key: tuple[str, int, str]) -> Spent:
        """从数据库把当前窗口的已用量读回来。不播种的话每次重启配额都归零，而重启在
        这个项目里就是升级方式。
        """
        subject_type, subject_id, window = key
        period = window_key(window)

        column = {
            "user": Run.user_id,
            "token": Run.token_id,
            "agent": Run.agent_id,
        }[subject_type]

        stmt = (
            select(
                func.count(Run.id),
                func.coalesce(func.group_concat(RunUsage.cost_usd), ""),
            )
            .select_from(Run)
            .outerjoin(RunUsage, RunUsage.run_id == Run.id)
            .where(column == subject_id)
        )
        start = window_start(window)
        if start is not None:
            stmt = stmt.where(Run.started_at >= start.isoformat())

        count, costs = (await session.execute(stmt)).one()
        # cost_usd 是 TEXT（Decimal 的 str），SQL 层没法 SUM，只能取回来自己加。
        # 窗口内的行数有限，而正确的金额比一次聚合查询重要。
        total = Decimal(0)
        for chunk in (costs or "").split(","):
            chunk = chunk.strip()
            if chunk:
                try:
                    total += Decimal(chunk)
                except Exception:
                    continue
        return Spent(period=period, usd=total, requests=int(count or 0))

    # ------------------------------------------------------------ 检查 + 预留
    def _keys_for(
        self, user_id: int, token_id: int | None, agent_id: int | None
    ) -> tuple[tuple[str, int, str], ...]:
        subjects: list[tuple[str, int]] = [("user", user_id)]
        if token_id is not None:
            subjects.append(("token", token_id))
        if agent_id is not None:
            subjects.append(("agent", agent_id))
        return tuple(
            (st, sid, w) for st, sid in subjects for w in WINDOWS if (st, sid, w) in self._rules
        )

    def reserve(
        self,
        *,
        user_id: int,
        token_id: int | None,
        agent_id: int | None,
    ) -> Reservation:
        """检查并占掉一个名额。超限抛 :class:`QuotaExceeded`。

        检查与占用在同一个同步块里完成、中间没有 ``await``，所以并发请求不可能全部通过
        检查再一起计数——不靠锁，靠单事件循环里不含 await 的代码块天然原子。

        最紧的那条先生效：三级主体逐个查，任意一条超了就拒。"给某个 token 单独设一个
        小额度"这种用法要成立，每一级都得真的拦。
        """
        keys = self._keys_for(user_id, token_id, agent_id)
        if not keys:
            return Reservation(self, ())

        # 先全部检查，再全部占用。混在一起的话，前几个已经 +1 而后面某个超限抛出，
        # 就会留下一批没人释放的名额。
        for key in keys:
            subject_type, _, window = key
            rule = self._rules[key]
            spent = self._spent.setdefault(key, Spent(period=window_key(window)))
            spent.roll_if_needed(window_key(window))

            if rule.limit_usd is not None and spent.usd >= rule.limit_usd:
                raise QuotaExceeded(subject_type, window, "usd")
            if rule.limit_requests is not None and spent.requests >= rule.limit_requests:
                raise QuotaExceeded(subject_type, window, "requests")

        for key in keys:
            self._spent[key].requests += 1

        return Reservation(self, keys)

    # ------------------------------------------------------------ 展示
    def snapshot(self) -> list[dict[str, object]]:
        """给管理面看的当前状态。"""
        out = []
        for key, rule in sorted(self._rules.items()):
            spent = self._spent.get(key, Spent(period=window_key(rule.window)))
            spent.roll_if_needed(window_key(rule.window))
            out.append(
                {
                    "subject_type": rule.subject_type,
                    "subject_id": rule.subject_id,
                    "window": rule.window,
                    "limit_usd": str(rule.limit_usd) if rule.limit_usd else None,
                    "limit_requests": rule.limit_requests,
                    "spent_usd": str(spent.usd),
                    "spent_requests": spent.requests,
                    "period": spent.period,
                }
            )
        return out

    @property
    def rule_count(self) -> int:
        return len(self._rules)


# =============================================================================
# 规则的增删
# =============================================================================


class InvalidQuota(ValueError):
    pass


async def upsert(
    session: AsyncSession,
    *,
    subject_type: str,
    subject_id: int,
    window: str,
    limit_usd: Decimal | None,
    limit_requests: int | None,
) -> None:
    """写入或更新一条规则。"""
    if subject_type not in SUBJECT_TYPES:
        raise InvalidQuota(f"主体类型只能是 {SUBJECT_TYPES} 之一")
    if window not in WINDOWS:
        raise InvalidQuota(f"窗口只能是 {WINDOWS} 之一")
    if limit_usd is None and limit_requests is None:
        raise InvalidQuota("金额上限与次数上限至少要设一个——两个都空等于没有配额")
    if limit_usd is not None and limit_usd <= 0:
        raise InvalidQuota("金额上限必须大于 0")
    if limit_requests is not None and limit_requests <= 0:
        raise InvalidQuota("次数上限必须大于 0")

    row = (
        await session.execute(
            select(Quota).where(
                Quota.subject_type == subject_type,
                Quota.subject_id == subject_id,
                Quota.window == window,
            )
        )
    ).scalar_one_or_none()

    if row is None:
        from ..db.models import utcnow

        session.add(
            Quota(
                subject_type=subject_type,
                subject_id=subject_id,
                window=window,
                limit_usd=str(limit_usd) if limit_usd is not None else None,
                limit_requests=limit_requests,
                created_at=utcnow(),
            )
        )
    else:
        row.limit_usd = str(limit_usd) if limit_usd is not None else None
        row.limit_requests = limit_requests


async def remove(session: AsyncSession, *, subject_type: str, subject_id: int, window: str) -> bool:
    row = (
        await session.execute(
            select(Quota).where(
                Quota.subject_type == subject_type,
                Quota.subject_id == subject_id,
                Quota.window == window,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    await session.delete(row)
    return True
