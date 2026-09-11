"""``xingcha doctor`` —— 一次性把常见的部署问题查出来。"""

from __future__ import annotations

import importlib.util
import os
import shutil
import stat as st
from pathlib import Path

import typer

from .. import __version__
from .. import contract as C
from ..config import get_settings
from ..db import migrate
from ._app import app
from ._ui import ok


@app.command()
def doctor() -> None:
    """体检：把常见的部署问题一次性查出来。"""
    settings = get_settings()
    problems = 0

    typer.secho(f"星槎 {__version__} · 契约 v{C.CONTRACT_VERSION}", bold=True)
    typer.echo()

    # --- 数据目录 ---
    typer.secho("数据", bold=True)
    typer.echo(f"  目录        {settings.data_dir.resolve()}")
    if settings.data_dir.exists():
        mode = st.S_IMODE(settings.data_dir.stat().st_mode)
        flag = "ok" if mode == C.DIR_MODE else f"应为 {C.DIR_MODE:o}"
        typer.echo(f"  目录权限    {mode:o} ({flag})")
        if mode != C.DIR_MODE:
            problems += 1
    else:
        typer.echo("  目录        尚未创建（首次 serve 时会建）")

    rev = migrate.current_revision(settings.db_path)
    head = migrate.head_revision()
    typer.echo(f"  schema      {rev or '(空库)'} / head {head}")
    if rev != head:
        typer.secho("              未迁移到最新，serve 启动时会自动升级", fg=typer.colors.YELLOW)

    typer.echo(f"  密钥环      {'存在' if settings.secret_path.exists() else '尚未创建'}")

    # --- 磁盘 ---
    try:
        u = shutil.disk_usage(settings.data_dir if settings.data_dir.exists() else Path("."))
        pct = u.free / u.total * 100
        typer.echo(f"  磁盘剩余    {u.free // 1024 // 1024} MB ({pct:.1f}%)")
        if pct < 10:
            typer.secho(
                "              磁盘不足 10%，SQLite 写入与迁移都会失败", fg=typer.colors.RED
            )
            problems += 1
    except OSError:
        pass

    # --- 网络与代理 ---
    typer.echo()
    typer.secho("网络", bold=True)

    proxy_vars = {
        k: v
        for k, v in os.environ.items()
        if k.lower() in {"all_proxy", "http_proxy", "https_proxy"}
    }
    # 这一段曾经把代理报成**问题**（"客户端一律 trust_env=False，不会继承它们"）。
    # 那是 trust_env 反转之前的说法，现在正相反——而 doctor 恰恰是撞上区域限制时
    # 第一个会去跑的命令，说反话等于把人推离根因。
    if proxy_vars:
        typer.secho("  出站代理：" + ", ".join(sorted(proxy_vars)), fg=typer.colors.GREEN)
        typer.echo("    星槎的出站客户端会走它（上游按出口 IP 挡请求时正需要）。")
        # find_spec 而不是 import：socksio 不是声明依赖，import 它连类型检查都过不了。
        if any(v.lower().startswith("socks") for v in proxy_vars.values()) and (
            importlib.util.find_spec("socksio") is None
        ):
            typer.secho(
                "    但这是 socks 代理，而 socksio 没装——客户端会在构造阶段 "
                "ImportError，星槎兜住之后**退回直连**，等于代理没生效。",
                fg=typer.colors.YELLOW,
            )
            typer.echo("    改用 http 代理，或 pip install httpx[socks]。")
    else:
        typer.secho("  未检测到出站代理环境变量", fg=typer.colors.YELLOW)
        typer.echo("    上游若按出口 IP 拒绝请求（not available in your region），配一个。")
        typer.echo("    容器里要写进 .env —— 宿主 shell 里 export 的容器看不见。")

    base_url = C.OPENROUTER_DEFAULT_BASE_URL
    typer.echo(f"  上游默认    {base_url}")

    # --- 运行约束 ---
    typer.echo()
    typer.secho("运行约束", bold=True)
    typer.echo(f"  worker      {C.REQUIRED_WORKERS}（硬约束，启动时断言）")
    typer.echo(f"  journal     {C.REQUIRED_JOURNAL_MODE}（启动时断言，否则拒绝启动）")
    typer.echo(f"  监听        {settings.host}:{settings.port}")
    typer.echo(f"  请求体上限  {C.MAX_BODY_BYTES // 1024 // 1024} MB")
    typer.echo(f"  并发上限    {settings.max_concurrency}")

    typer.echo()
    if problems:
        typer.secho(f"发现 {problems} 个问题。", fg=typer.colors.RED)
        raise typer.Exit(1)
    ok("没有发现问题。")
