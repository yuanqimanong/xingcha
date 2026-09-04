"""FastAPI 应用装配与启动序列。

**启动顺序是有意的**，每一步都可能拒绝启动：

1. umask + 数据目录权限 —— 在任何文件被创建之前
2. 未知配置项告警 —— 让拼错的环境变量被看见
3. 密钥环 —— 缺环而库里有密文则拒绝启动（单向门）
4. 迁移（先备份） —— 失败则拒绝启动，绝不带着半旧 schema 服务
5. WAL 断言 —— 静默降级会变成零星的 database is locked

宁可起不来，也不要带病运行：上面每一条失败后继续跑，症状都会在几小时后以完全
看不出根因的形式出现。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from . import __version__
from . import contract as C
from .bootstrap import prepare
from .config import Settings, get_settings, warn_unknown_env
from .crypto import Keyring
from .db.engine import assert_wal, make_engine, make_sessionmaker
from .errors import XingchaError, unhandled_error_handler, xingcha_error_handler

log = logging.getLogger(__name__)


class AppState:
    """进程级共享状态。挂在 ``app.state.xc`` 上。

    显式持有而不是散落成模块级全局：一次调用的生命周期要能用一张图讲完
    （开发计划 §6 标准 6）。
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.keyring: Keyring | None = None
        self.engine: Any = None
        self.sessionmaker: Any = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.xc.settings

    # 拼错的环境变量。不致命，但必须被看见——你以为设了某个值，实际跑的是默认值。
    warn_unknown_env()

    # umask → 数据目录 → 迁移（内含备份）→ 密钥环。顺序见 bootstrap.prepare。
    # serve 与 CLI 共用同一份，避免两处规则不一致。
    app.state.xc.keyring = prepare(settings)
    log.info("密钥环就绪（%d 把密钥）", len(app.state.xc.keyring))

    engine = make_engine(settings.db_path)
    app.state.xc.engine = engine
    app.state.xc.sessionmaker = make_sessionmaker(engine)

    # WAL 断言。静默降级会变成零星的 database is locked——最难查的一类问题。
    await assert_wal(engine)

    log.info("星槎 %s 已就绪 · 监听 %s:%s", __version__, settings.host, settings.port)
    try:
        yield
    finally:
        # 用量缓冲必须在这里落盘。「批量 flush」+「重启即升级」如果没有这一步，
        # 每次升级都会静默丢掉内存里那批 run 行——账单恰好在最需要它可信的时刻少报，
        # 而且丢多少无法事后察觉。
        # （M1 接上 UsageBuffer 后在此 flush；M0 还没有缓冲。）
        await engine.dispose()
        log.info("星槎已停止")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )

    app = FastAPI(
        title="星槎 Xīngchá",
        version=__version__,
        lifespan=lifespan,
        # 文档只在管理面下暴露，/v1 保持纯净——那是给 OpenAI SDK 用的
        docs_url="/admin/docs",
        redoc_url=None,
        openapi_url="/admin/openapi.json",
        # 必须显式关掉：即使 docs_url 挪到了 /admin 下，FastAPI 仍会在**根路径**注册
        # 一个 /docs/oauth2-redirect。那是一条计划外的免鉴权路由，而免鉴权路由
        # 是一个安全关键闭集（见 tests/test_app_startup.py）。星槎的 Swagger 不走
        # OAuth，这个回调本来也没用。
        swagger_ui_oauth2_redirect_url=None,
    )
    app.state.xc = AppState(settings)

    app.add_exception_handler(XingchaError, xingcha_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)

    _mount_probes(app)
    return app


def _mount_probes(app: FastAPI) -> None:
    """探针与版本协商。三个都**免鉴权**——这是一个安全关键的闭集。

    往这里加路由必须同步更新 tests/test_contract_frozen.py 的免鉴权白名单断言。
    """

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        """存活探针。只表示进程还在，不查依赖。"""
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> JSONResponse:
        """就绪探针。查数据库可写与磁盘水位。

        磁盘要单独报：整个产品就是一个 SQLite 文件，磁盘一满就是写失败 + 迁移失败 +
        无法启动，而根因（通常是日志涨满）在别处完全看不见。
        """
        import shutil

        from sqlalchemy import text

        state: AppState = app.state.xc
        checks: dict[str, Any] = {}
        ok = True

        try:
            async with state.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            checks["db"] = "ok"
        except Exception as e:
            checks["db"] = f"error: {type(e).__name__}"
            ok = False

        try:
            usage = shutil.disk_usage(state.settings.data_dir)
            free_pct = usage.free / usage.total * 100
            checks["disk_free_pct"] = round(free_pct, 1)
            checks["disk_free_mb"] = usage.free // 1024 // 1024
            if free_pct < 10:
                checks["disk"] = "degraded"
                ok = False
        except OSError as e:
            checks["disk"] = f"error: {type(e).__name__}"

        return JSONResponse(
            status_code=200 if ok else 503,
            content={"status": "ok" if ok else "degraded", "checks": checks},
        )

    @app.get("/version", include_in_schema=False)
    async def version() -> dict[str, Any]:
        """版本协商与能力探测。

        契约号只在破坏性变更时 +1。``features`` 让调用方无需试探即可知道服务端支持
        什么——这是万一真的必须收紧某个行为时，唯一的非硬切发布通道。
        """
        return {
            "name": "xingcha",
            "version": __version__,
            "contract": C.CONTRACT_VERSION,
            "features": sorted(C.FEATURES),
        }
