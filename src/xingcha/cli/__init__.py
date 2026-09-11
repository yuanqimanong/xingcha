"""命令行入口。

一个命令组一个模块；这里只负责 import 它们（``@app.command()`` 在 import 时完成
注册）与放两条不属于任何组的顶层命令。**命令集是闭集**——脚本会
依赖这些命令名，改名等于毁约。

    xingcha serve      起服务
    xingcha config     读写服务端配置（首次部署的唯一入口，见 :mod:`.config`）
    xingcha token      签发 / 吊销 sk-xc- 令牌
    xingcha agent      查看 / 导出 / 导入 Agent
    xingcha db         迁移、备份与恢复
    xingcha admin      后台账号
    xingcha quota      配额
    xingcha doctor     体检
    xingcha version    版本与契约号
"""

from __future__ import annotations

from typing import Annotated

import typer

from .. import __version__
from .. import contract as C
from ..config import get_settings

# 这些模块只为副作用而 import：模块级的 @xxx_app.command() 就是注册动作。
from . import admin, agent, config, db, doctor, quota, token  # noqa: F401
from ._app import app
from ._ui import run_sync, use_utf8_output

__all__ = ["app"]

# 必须在任何命令输出之前。理由见 use_utf8_output——Windows 上少了它，第一句
# 带 ✓ 的输出就会让整条命令崩掉。
use_utf8_output()


@app.command()
def serve(
    host: Annotated[str | None, typer.Option(help="监听地址。默认 127.0.0.1。")] = None,
    port: Annotated[int | None, typer.Option(help="监听端口。")] = None,
    reload: Annotated[bool, typer.Option(help="改代码自动重启（仅开发用）。")] = False,
) -> None:
    """启动服务。

    启动时会自动跑数据库迁移（迁移前先备份），并做一系列拒绝启动的断言——
    见 app.lifespan 的注释。
    """
    import uvicorn

    settings = get_settings()
    bind_host = host or settings.host
    bind_port = port or settings.port

    # 数据目录预检。真正的建目录在 app.lifespan 里，但那已经在 uvicorn 之内——
    # 失败会以一段 ASGI 栈回溯的形式出现，而运维需要的那句话被埋在最底下。
    # 这里先跑一次（幂等），让它走 CLI 的运维错误通道：只印一行可执行的指引。
    run_sync(settings.ensure_data_dir)

    if bind_host == "0.0.0.0":
        typer.secho(
            "⚠ 正在监听 0.0.0.0。星槎默认只监听 127.0.0.1；对外暴露时应由反代前置、"
            "容器不映射宿主端口。\n"
            "  注意 Docker 的 DOCKER-USER 链会绕过 ufw：即使防火墙规则写了 deny，"
            "映射出去的端口照样可达。",
            fg=typer.colors.YELLOW,
            err=True,
        )

    # 只有显式配了信任范围才读 X-Forwarded-*。见 Settings.trusted_proxies：
    # 默认谁都不信，因为那个头决定 cookie 要不要带 Secure。
    trusted = settings.trusted_proxies
    if trusted:
        typer.secho(f"→ 信任来自 {trusted} 的 X-Forwarded-* 头", fg=typer.colors.CYAN, err=True)

    uvicorn.run(
        "xingcha.app:create_app",
        factory=True,
        host=bind_host,
        port=bind_port,
        reload=reload,
        proxy_headers=bool(trusted),
        forwarded_allow_ips=trusted or "",
        # 单 worker 是硬约束：进程级并发上限、用量缓冲、SQLite 单写者全都依赖它。
        workers=C.REQUIRED_WORKERS,
        log_config=None,
        access_log=False,  # access log 会把 header 写进日志，那是一条 key 泄漏路径
    )


@app.command()
def version() -> None:
    """打印版本与契约号。"""
    typer.echo(f"xingcha {__version__} (contract v{C.CONTRACT_VERSION})")


if __name__ == "__main__":
    app()
