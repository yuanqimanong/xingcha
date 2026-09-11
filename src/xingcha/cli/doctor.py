"""``xingcha doctor`` —— 一次性把常见的部署问题查出来。"""

from __future__ import annotations

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
    if proxy_vars:
        typer.secho(
            "  检测到机器级代理环境变量：" + ", ".join(sorted(proxy_vars)),
            fg=typer.colors.YELLOW,
        )
        typer.echo("    星槎自建的 HTTP 客户端一律 trust_env=False，不会继承它们。")
        typer.echo("    要走中转请配 openrouter.base_url，不要依赖机器代理。")
        if any(v.startswith("socks") for v in proxy_vars.values()):
            typer.secho(
                "    注意：socks5 代理会让未关闭 trust_env 的客户端在构造阶段直接 "
                "ImportError（socksio 未装），且报错完全看不出跟代理有关。",
                fg=typer.colors.YELLOW,
            )
    else:
        typer.echo("  未检测到机器级代理环境变量")

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
