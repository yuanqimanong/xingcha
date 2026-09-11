"""``xingcha quota`` —— 设置与查看配额。"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

import typer

from ..db.engine import session_scope
from ..services import quota as quota_svc
from ..services.quota import QuotaService
from ._app import quota_app
from ._ui import bootstrap, err, info, ok, pad, run_async


@quota_app.command("set")
def quota_set(
    subject: Annotated[str, typer.Argument(help="主体，形如 user:1 / token:3 / agent:2。")],
    window: Annotated[str, typer.Argument(help="窗口：day / month / total。")],
    usd: Annotated[float | None, typer.Option("--usd", help="金额上限（美元）。")] = None,
    requests: Annotated[int | None, typer.Option("--requests", help="次数上限。")] = None,
) -> None:
    """设一条配额。

    例：

        xingcha quota set user:1 day --usd 5
        xingcha quota set token:3 month --requests 10000

    三级主体逐个检查，**最紧的那条先生效**。金额上限与次数上限至少要设一个。
    """

    if ":" not in subject:
        err("主体要写成 <类型>:<id>，例如 user:1")
        raise typer.Exit(1)
    subject_type, _, raw_id = subject.partition(":")
    if not raw_id.isdigit():
        err(f"主体 id 必须是数字，收到 {raw_id!r}")
        raise typer.Exit(1)

    async def run() -> None:
        engine, maker, _ = bootstrap()
        try:
            async with session_scope(maker) as s:  # type: ignore[arg-type]
                await quota_svc.upsert(
                    s,
                    subject_type=subject_type,
                    subject_id=int(raw_id),
                    window=window,
                    limit_usd=Decimal(str(usd)) if usd is not None else None,
                    limit_requests=requests,
                )
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    try:
        run_async(run())
    except quota_svc.InvalidQuota as e:
        err(str(e))
        raise typer.Exit(1) from e

    ok(f"已设置 {subject} 的 {window} 配额")
    typer.secho("  重启服务或在后台保存一次让它生效。", fg=typer.colors.CYAN)


@quota_app.command("list")
def quota_list() -> None:
    """列出配额规则与当前窗口的已用量。"""

    async def run() -> list:
        engine, maker, _ = bootstrap()
        try:
            svc = QuotaService(maker)  # type: ignore[arg-type]
            await svc.reload()
            return svc.snapshot()
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    rows = run_async(run())
    if not rows:
        info("还没有配额规则。设一条：xingcha quota set user:1 day --usd 5")
        typer.secho(
            "  提醒：没有配额时，唯一的钱刹车是 OpenRouter 侧那把 key 的信用上限。",
            fg=typer.colors.YELLOW,
        )
        return

    typer.secho(
        pad("主体", 16) + pad("窗口", 10) + pad("已用 / 上限（USD）", 30) + "已用 / 上限（次）",
        bold=True,
    )
    for r in rows:
        subject = f"{r['subject_type']}:{r['subject_id']}"
        usd = f"{r['spent_usd']} / {r['limit_usd'] or '—'}"
        cnt = f"{r['spent_requests']} / {r['limit_requests'] or '—'}"
        over = (r["limit_usd"] is not None and float(r["spent_usd"]) >= float(r["limit_usd"])) or (
            r["limit_requests"] is not None and r["spent_requests"] >= r["limit_requests"]
        )
        typer.secho(
            pad(subject, 16) + pad(str(r["window"]), 10) + pad(usd, 30) + cnt,
            fg=typer.colors.RED if over else None,
        )


@quota_app.command("unset")
def quota_unset(
    subject: Annotated[str, typer.Argument(help="主体，形如 user:1。")],
    window: Annotated[str, typer.Argument(help="窗口。")],
) -> None:
    """删掉一条配额。"""

    subject_type, _, raw_id = subject.partition(":")

    async def run() -> bool:
        engine, maker, _ = bootstrap()
        try:
            async with session_scope(maker) as s:  # type: ignore[arg-type]
                return await quota_svc.remove(
                    s, subject_type=subject_type, subject_id=int(raw_id), window=window
                )
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    if run_async(run()):
        ok(f"已删除 {subject} 的 {window} 配额")
    else:
        info("没有这条规则")
