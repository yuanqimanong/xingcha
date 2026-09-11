"""``xingcha admin`` —— 后台账号。密码忘了从这里重置。"""

from __future__ import annotations

from typing import Annotated

import typer
from sqlalchemy import func, select

from .. import contract as C
from ..config import get_settings
from ..db.engine import session_scope
from ..db.models import WebSession
from ..services import websession as ws
from ._app import admin_app
from ._ui import bootstrap, err, info, ok, run_async


@admin_app.command("reset-password")
def admin_reset_password(
    yes: Annotated[bool, typer.Option("--yes", help="跳过确认。")] = False,
) -> None:
    """清空后台密码，下次访问 /admin 会回到「设置管理员密码」。

    ------------------------------------------------------------------------
    为什么需要这条命令
    ------------------------------------------------------------------------

    后台密码是**唯一凭据**，而它此前**没有任何可运维的恢复路径**：CLI 里没有重置
    入口，代码里也没有别的办法。忘了密码的唯一答案是"去手改 SQLite"——那既没写进
    runbook，也不是一个能让人在半夜照着做的操作。

    对单用户自托管来说"某天忘了密码"不是小概率事件。

    ------------------------------------------------------------------------
    为什么是清空而不是设一个新的
    ------------------------------------------------------------------------

    设新密码就得把它交给命令行——于是它进 shell 历史、进 ``ps`` 输出、可能进
    终端录屏。清空之后走浏览器的首次设密流程，密码从头到尾只经过 HTTPS 表单。

    **现有会话一并失效。** 不失效的话，一个已登录的浏览器仍然握着完整权限，而你
    执行这条命令的场景往往正是"我不确定还有谁登着"。
    """
    settings = get_settings()

    async def run() -> int:
        engine, maker, _ = bootstrap()
        try:
            async with session_scope(maker) as s:  # type: ignore[arg-type]
                admin = await ws.get_admin(s)
                if admin is None:
                    err("数据库里没有管理员账号。库是不是空的？")
                    raise typer.Exit(1)

                # 环境变量生效时**屏蔽重置**。
                #
                # 这时库里本来就没有密码（那正是环境变量生效的条件），清空是个空操作；
                # 而它会让人以为"重置了、可以重新设一个"——实际下次登录仍然按环境变量
                # 校验。与其做一个没有效果的动作，不如说清楚该去改哪儿。
                if ws.env_password_in_effect(admin.password_hash, settings.admin_password):
                    err(
                        "密码由环境变量 XINGCHA_ADMIN_PASSWORD 托管，重置在这里没有意义。\n"
                        "  要换密码：改 .env 里的那一项并重启服务。\n"
                        "  要改回后台设密：删掉那一项并重启，然后访问 /admin 设定。"
                    )
                    raise typer.Exit(2)

                if not yes:
                    typer.confirm(
                        "清空后台密码？现有登录会话会全部失效，下次访问 /admin 需要重新设定密码。",
                        abort=True,
                    )
                admin.password_hash = None
                # 与后台改密码共用同一个吊销实现（架构标准 2：一个概念一处定义）
                return await ws.revoke_all(s)
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    killed = run_async(run())
    ok(f"密码已清空，{killed} 个会话已失效")
    typer.secho(
        f"  下一步：打开 {settings.public_url or 'https://<你的域名>'}/admin 重新设定密码。\n"
        f"  密码至少 {C.MIN_ADMIN_PASSWORD_LEN} 位，"
        "且不要复用其它服务的——这个后台能改写上游 base_url。",
        fg=typer.colors.CYAN,
    )


@admin_app.command("status")
def admin_status() -> None:
    """看后台账号的状态：密码设了没、有几个活跃会话。"""

    async def run() -> tuple[bool, int]:
        engine, maker, _ = bootstrap()
        try:
            async with session_scope(maker) as s:  # type: ignore[arg-type]
                admin = await ws.get_admin(s)
                n = (await s.execute(select(func.count()).select_from(WebSession))).scalar() or 0
                return bool(admin and admin.password_hash), int(n)
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    has_password, sessions = run_async(run())

    settings = get_settings()
    env_managed = ws.env_password_in_effect("x" if has_password else None, settings.admin_password)
    if env_managed:
        info("密码：由环境变量 XINGCHA_ADMIN_PASSWORD 托管")
        typer.secho(
            "  要换密码就改 .env 并重启；后台与 CLI 的改/重置都不生效。", fg=typer.colors.CYAN
        )
    elif has_password:
        info("密码：已设置（存在库里）")
        if settings.admin_password:
            typer.secho(
                "  环境变量 XINGCHA_ADMIN_PASSWORD **被忽略**：库里已有密码，先立者为准。\n"
                "  要改用它，先跑 `xingcha admin reset-password`。",
                fg=typer.colors.YELLOW,
            )
    else:
        info("密码：未设置（下次访问 /admin 会引导设定）")
    info(f"活跃会话：{sessions}")
