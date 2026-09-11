"""调用记录页。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import Response
from sqlalchemy import func, select

from ...db.models import Run
from .render import render
from .runs import recent_runs
from .security import (
    require_admin,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", include_in_schema=False)


@router.get("/logs")
async def logs_page(request: Request) -> Response:
    await require_admin(request)
    state = request.app.state.xc
    model = request.query_params.get("model", "").strip()
    status = request.query_params.get("status", "").strip()

    async with state.sessionmaker() as s:
        runs = await recent_runs(s, limit=200, model=model, status=status)
        total = (await s.execute(select(func.count(Run.id)))).scalar_one()

    return render(
        request,
        "logs.html",
        {"runs": runs, "total": total, "filters": {"model": model, "status": status}},
    )
