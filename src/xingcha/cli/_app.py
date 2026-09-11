"""Typer 应用对象。

单独一个模块，是为了打破一个环：``cli/__init__`` 要 import 各个命令模块来完成
注册，而命令模块又要拿到自己挂靠的那个 ``Typer``。两边都指向这里就不成环了。

**命令集是闭集（契约 §3.13）**：脚本会依赖这些命令名，改名等于毁约。加子命令
可以，改名或删名不行。
"""

from __future__ import annotations

import typer

app = typer.Typer(
    name="xingcha",
    help="星槎 —— 把提示词变成可调用、可计量、可带走的服务。",
    no_args_is_help=True,
    add_completion=False,
)
config_app = typer.Typer(help="读写服务端配置（上游 key 等）。", no_args_is_help=True)
db_app = typer.Typer(help="数据库迁移与备份。", no_args_is_help=True)
token_app = typer.Typer(help="签发、查看与吊销 API 令牌。", no_args_is_help=True)
agent_app = typer.Typer(help="查看与导出 Agent。", no_args_is_help=True)
quota_app = typer.Typer(help="设置与查看配额。", no_args_is_help=True)
admin_app = typer.Typer(help="管理后台账号。", no_args_is_help=True)

app.add_typer(config_app, name="config")
app.add_typer(db_app, name="db")
app.add_typer(token_app, name="token")
app.add_typer(agent_app, name="agent")
app.add_typer(quota_app, name="quota")
app.add_typer(admin_app, name="admin")
