"""调用记录的查询与聚合。

总览、密钥详情、调用记录三页问的是同一批问题（这段时间跑了多少次、花了多少、错在哪），只是过滤条件不同。放一处，三页的口径才不会各自漂。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from sqlalchemy import ColumnElement, func, select
from sqlalchemy import case as sa_case

from ... import contract as C
from ...db.models import Run, RunUsage
from .render import COST_HINT, fmt_cost, fmt_time

log = logging.getLogger(__name__)


def since(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")


#: 从来没被调用过的 Agent，列表页照样要显示——GROUP BY 里没有它的行。
NO_RUNS = SimpleNamespace(total=0, ok=0, failed=0, ok_rate="—", cost="—", unpriced=0, last="—")


#: 失败原因的中文标签。**照 contract.ErrorType 逐项写，不做兜底翻译。**
#:
#: 兜底翻译（比如把下划线换成空格）会让一个新增的 error type 看起来像是被支持的，
#: 而实际上没人为它想过该说什么。缺的那一项直接显示原值，缺失一眼可见。
ERROR_LABELS: dict[str, str] = {
    C.ErrorType.INVALID_API_KEY.value: "密钥无效",
    C.ErrorType.QUOTA_EXCEEDED.value: "超出配额",
    C.ErrorType.MODEL_NOT_FOUND.value: "模型不存在",
    C.ErrorType.MODEL_INVALID.value: "请求不合法",
    C.ErrorType.PARAM_UNSUPPORTED.value: "参数不支持",
    C.ErrorType.STREAM_UNSUPPORTED.value: "该 Agent 不支持流式",
    C.ErrorType.REQUEST_TOO_LARGE.value: "请求过大",
    C.ErrorType.SCHEMA_VIOLATION.value: "输出不合 schema（重试已耗尽）",
    C.ErrorType.AGENT_SPEC_INVALID.value: "Agent 定义不合法",
    C.ErrorType.AGENT_BUILD_FAILED.value: "Agent 无法构造",
    C.ErrorType.UPSTREAM_ERROR.value: "上游报错",
    C.ErrorType.UPSTREAM_TIMEOUT.value: "上游超时",
    C.ErrorType.REQUEST_TIMEOUT.value: "整轮超时",
    C.ErrorType.INTERNAL_ERROR.value: "服务内部错误",
}


async def run_stats(
    s, *, token_id: int | None = None, agent_id: int | None = None, since: str | None = None
) -> Any:
    """一组主体的调用统计。总览与单把密钥 **共用这一份**。

    写三份的话，三处对"成功率"的定义迟早会分叉——而分叉之后没人知道该信哪个数。

    这里挑的几个比率，每一个都对应一个会花钱或会骗人的具体现象：

    * **成功率** —— 最直白的那个。
    * **重试放大** = 上游请求数 / 调用数。结构化 Agent 一次调用最坏打 1+N 次上游，
      而账单按整轮算。这个数从 1.0 涨上去，就是钱在往上走。
    * **schema 违规率** —— 违规就是重试，重试就是钱。它比成功率更早预警：输出
      质量在退化时，成功率还是 100%（重试兜住了），只有这个数会先动。
    * **可定价率** —— 约三分之一的在售模型查不到价，对它们费用记的是 NULL。
      不把这个数摆出来，"这个月花了 X" 就是一句**不知道漏了多少**的话。
    * **缓存命中率** —— 只在上游报了 cached_tokens 时有意义，报了就是真省钱。

    ``since`` 把口径收进一个时间窗。**总览要传它**：全时段的成功率是个没人看的
    数——半年前坏过一周，这个数就再也回不来了，看不出现在好不好。它同时也是性能
    上的必需，费用那一段是唯一按行进 Python 的（Decimal 不能交给 SQLite 的 SUM
    去算，那会把它变成 float），实测 10 万行要 0.24 秒。

    **不要按 Agent 循环调它。** 列表页用 :func:`agent_summaries` 的一次 GROUP BY；
    50 个 Agent 各跑一遍这个函数实测 1.7 秒，而列表页只需要其中四个数。
    """
    where = []
    if token_id is not None:
        where.append(Run.token_id == token_id)
    if agent_id is not None:
        where.append(Run.agent_id == agent_id)
    if since is not None:
        where.append(Run.started_at >= since)

    row = (
        await s.execute(
            select(
                func.count(Run.id),
                func.sum(sa_case((Run.status == "ok", 1), else_=0)),
                func.coalesce(func.sum(RunUsage.input_tokens), 0),
                func.coalesce(func.sum(RunUsage.output_tokens), 0),
                func.coalesce(func.sum(RunUsage.cache_read_tokens), 0),
                func.coalesce(func.sum(RunUsage.requests), 0),
                func.coalesce(func.sum(RunUsage.schema_violations), 0),
                func.min(Run.started_at),
                func.max(Run.started_at),
                func.avg(Run.latency_ms),
            )
            .select_from(Run)
            .outerjoin(RunUsage, RunUsage.run_id == Run.id)
            .where(*where)
        )
    ).one()
    total, ok, tin, tout, tcache, treq, tviol, first, last, avg_ms = row
    ok = int(ok or 0)

    # 费用与"能不能定价"必须一起取。只加总非 NULL 的话，得到的是一个看起来精确、
    # 实际不知道漏了多少的数。
    priced, unpriced, cost = 0, 0, Decimal(0)
    rows = (
        await s.execute(
            select(RunUsage.cost_usd)
            .select_from(Run)
            .join(RunUsage, RunUsage.run_id == Run.id)
            .where(*where)
        )
    ).scalars()
    for raw in rows:
        if raw is None:
            unpriced += 1
        else:
            priced += 1
            cost += Decimal(raw)

    errors = (
        await s.execute(
            select(Run.error_type, func.count(Run.id))
            .where(Run.status != "ok", *where)
            .group_by(Run.error_type)
            .order_by(func.count(Run.id).desc())
        )
    ).all()

    def pct(n: int, d: int) -> str:
        return f"{n * 100 / d:.1f}%" if d else "—"

    return SimpleNamespace(
        total=total,
        ok=ok,
        failed=total - ok,
        ok_rate=pct(ok, total),
        # 失败率单列一个数：把它算成 100% - 成功率 是在页面上做减法，
        # 而两个数各自四舍五入之后加起来不一定是 100。
        fail_rate=pct(total - ok, total),
        amplification=f"{treq / total:.2f}×" if total and treq else "—",
        violation_rate=pct(int(tviol or 0), total),
        violations=int(tviol or 0),
        priced_rate=pct(priced, priced + unpriced),
        unpriced=unpriced,
        cache_rate=pct(int(tcache or 0), int(tin or 0)),
        input_tokens=int(tin or 0),
        output_tokens=int(tout or 0),
        cost=fmt_cost(str(cost)) if priced else "—",
        avg_ms=f"{int(avg_ms)} ms" if avg_ms else "—",
        first=fmt_time(first) if first else "—",
        last=fmt_time(last) if last else "—",
        errors=[
            SimpleNamespace(
                type=t or "未记录", count=c, label=ERROR_LABELS.get(t or "", t or "未记录")
            )
            for t, c in errors
        ],
    )


async def agent_summaries(s) -> dict[int, Any]:
    """所有 Agent 的四个摘要数，**一次查询**。

    列表页只显示"调了多少次 / 成功率 / 花了多少 / 最近一次"。按 Agent 循环调
    :func:`run_stats` 能得到同样的数，但那是 3×N 次查询、其中一次还按行进 Python
    ——实测 50 个 Agent × 10 万行 run 要 1.7 秒，而这一次 GROUP BY 是 0.06 秒。

    费用在这里交给 SQL 的 ``SUM`` 算，也就是走 REAL。存储层坚持 TEXT 是因为
    "float 存不住 Decimal"，那说的是**存**；这里是一个只用于展示的合计，量级在
    1e-4 美元、有效数字六位，float 的累积误差落在显示精度之外好几个数量级。
    要精确值的地方（配额结算）走的是内存计数器，不是这条路。
    """
    rows = (
        await s.execute(
            select(
                Run.agent_id,
                func.count(Run.id),
                func.sum(sa_case((Run.status == "ok", 1), else_=0)),
                func.max(Run.started_at),
                func.sum(RunUsage.cost_usd),
                func.sum(sa_case((RunUsage.cost_usd.is_(None), 1), else_=0)),
            )
            .select_from(Run)
            .outerjoin(RunUsage, RunUsage.run_id == Run.id)
            .where(Run.agent_id.is_not(None))
            .group_by(Run.agent_id)
        )
    ).all()
    out: dict[int, Any] = {}
    for aid, total, ok, last, cost, unpriced in rows:
        ok = int(ok or 0)
        out[int(aid)] = SimpleNamespace(
            total=total,
            ok=ok,
            failed=total - ok,
            ok_rate=f"{ok * 100 / total:.1f}%" if total else "—",
            cost=fmt_cost(str(cost)) if cost else "—",
            unpriced=int(unpriced or 0),
            last=fmt_time(last) if last else "—",
        )
    return out


async def run_sources(s, *, token_id: int | None = None, limit: int = 20) -> list[Any]:
    """按来源聚合。**key 泄漏时第一个要回答的问题是"它现在被谁在用"。**

    只看调用记录一行行翻答不了——要的是"有几个来源、各调了多少、最近一次什么
    时候"。一把本该只给一台服务器用的 key 上突然冒出第二个 IP，这张表一眼能看出来。
    """
    # 标注类型：不标的话列表类型被第一个元素（BinaryExpression）定死，而 `==`
    # 产生的是更宽的 ColumnElement，append 就成了类型错误。
    where: list[ColumnElement[bool]] = [Run.client_ip.is_not(None)]
    if token_id is not None:
        where.append(Run.token_id == token_id)
    rows = (
        await s.execute(
            select(
                Run.client_ip,
                Run.user_agent,
                func.count(Run.id),
                func.max(Run.started_at),
                func.sum(sa_case((Run.status == "ok", 1), else_=0)),
            )
            .where(*where)
            .group_by(Run.client_ip, Run.user_agent)
            .order_by(func.max(Run.started_at).desc())
            .limit(limit)
        )
    ).all()
    return [
        SimpleNamespace(
            ip=ip,
            agent=ua or "—",
            count=n,
            last=fmt_time(last),
            failed=n - int(ok or 0),
        )
        for ip, ua, n, last, ok in rows
    ]


async def recent_runs(
    s,
    *,
    limit: int,
    model: str = "",
    status: str = "",
    token_id: int | None = None,
    agent_id: int | None = None,
) -> list[Any]:
    stmt = (
        select(Run, RunUsage)
        .outerjoin(RunUsage, RunUsage.run_id == Run.id)
        .order_by(Run.started_at.desc())
        .limit(limit)
    )
    if model:
        stmt = stmt.where(Run.model.like(f"%{model}%"))
    if status == "ok":
        stmt = stmt.where(Run.status == "ok")
    elif status == "error":
        stmt = stmt.where(Run.status != "ok")
    if token_id is not None:
        stmt = stmt.where(Run.token_id == token_id)
    if agent_id is not None:
        stmt = stmt.where(Run.agent_id == agent_id)

    out = []
    for run, usage in (await s.execute(stmt)).all():
        out.append(
            {
                "started_at": fmt_time(run.started_at),
                "model": run.model,
                "status": run.status,
                "error_type": run.error_type,
                "input_tokens": usage.input_tokens if usage else 0,
                "output_tokens": usage.output_tokens if usage else 0,
                "cache_read_tokens": usage.cache_read_tokens if usage else 0,
                "cost_display": fmt_cost(usage.cost_usd if usage else None),
                "cost_source": usage.cost_source if usage else "unknown",
                "cost_hint": COST_HINT.get(usage.cost_source if usage else "unknown", "来源未知"),
                "latency_display": f"{run.latency_ms} ms" if run.latency_ms is not None else "—",
                "client_ip": run.client_ip or "—",
                "user_agent": run.user_agent or "",
            }
        )
    return out
