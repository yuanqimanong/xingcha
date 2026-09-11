"""总览页。"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response
from sqlalchemy import func, select

from ...db.models import Run, RunUsage
from .render import fmt_cost, render
from .runs import recent_runs, run_stats, since
from .security import (
    require_admin,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", include_in_schema=False)


@router.get("")
@router.get("/")
async def overview(request: Request) -> Response:
    await require_admin(request)
    state = request.app.state.xc

    async with state.sessionmaker() as s:
        day, week = since(1), since(7)

        async def agg(since: str) -> dict[str, Any]:
            row = (
                await s.execute(
                    select(
                        func.count(Run.id),
                        func.coalesce(func.sum(RunUsage.input_tokens), 0),
                        func.coalesce(func.sum(RunUsage.output_tokens), 0),
                    )
                    .select_from(Run)
                    .outerjoin(RunUsage, RunUsage.run_id == Run.id)
                    .where(Run.started_at >= since)
                )
            ).one()
            costs = (
                (
                    await s.execute(
                        select(RunUsage.cost_usd)
                        .select_from(Run)
                        .join(RunUsage, RunUsage.run_id == Run.id)
                        .where(Run.started_at >= since, RunUsage.cost_usd.is_not(None))
                    )
                )
                .scalars()
                .all()
            )
            total = sum((Decimal(c) for c in costs), Decimal(0))
            return {"runs": row[0], "input": row[1], "output": row[2], "cost": total}

        d, w = await agg(day), await agg(week)
        # **收在 30 天窗口里。** 全时段的成功率是个没人看的数：半年前坏过一周，
        # 它就再也回不来了，看不出现在好不好。也是性能上的必需，见 run_stats。
        stats = await run_stats(s, since=since(30))
        runs = await recent_runs(s, limit=8)

    today = {
        "today_runs": d["runs"],
        "week_runs": w["runs"],
        "today_cost": fmt_cost(str(d["cost"])),
        "week_cost": fmt_cost(str(w["cost"])),
        "today_tokens": d["input"] + d["output"],
        "today_input": d["input"],
        "today_output": d["output"],
    }
    return render(
        request,
        "overview.html",
        {
            "today": today,
            "stats": stats,
            "runs": runs,
            "upstream_configured": state.upstream.configured,
        },
    )
