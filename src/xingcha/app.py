"""FastAPI 应用装配与启动序列。

**启动顺序是有意的**，每一步都可能拒绝启动：

1. 未知配置项告警 —— 让拼错的环境变量被看见
2. umask + 数据目录权限 —— 在任何文件被创建之前
3. 迁移（先备份） —— 失败则拒绝启动，绝不带着半旧 schema 服务
4. 密钥环 —— 缺环而库里有密文则拒绝启动（单向门）
5. WAL 断言 —— 静默降级会变成零星的 database is locked
6. 上游配置 —— 缺 key **不**拒绝启动（首次部署必然缺），调用时才明确报错

宁可起不来，也不要带病运行：前五条失败后继续跑，症状都会在几小时后以完全
看不出根因的形式出现。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from . import __version__
from . import contract as C
from .api import v1 as v1_api
from .api.normalize import NormalizeV1Path
from .bootstrap import prepare
from .config import Settings, get_settings, warn_unknown_env
from .core.costsink import CostSink
from .core.models_catalog import ModelsCatalog
from .core.upstream import UpstreamConfig, UpstreamNotConfigured, UpstreamPool
from .crypto import Keyring
from .db.engine import assert_wal, make_engine, make_sessionmaker
from .errors import XingchaError, unhandled_error_handler, xingcha_error_handler
from .services import setting as setting_svc
from .services.quota import QuotaService
from .services.ratelimit import RateLimiter
from .services.run import RuntimeCache
from .services.runlog import UsageBuffer
from .web import routes as web_routes

log = logging.getLogger(__name__)


class AppState:
    """进程级共享状态。挂在 ``app.state.xc`` 上。

    显式持有而不是散落成模块级全局：一次调用的生命周期要能用一张图讲完
    （开发计划 §6 标准 6），而全局变量很难说清"该在哪儿失效它"。
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.keyring: Keyring | None = None
        self.engine: Any = None
        self.sessionmaker: Any = None
        self.upstream = UpstreamPool(timeout=settings.request_timeout)
        self.catalog = ModelsCatalog(ttl_seconds=settings.catalog_ttl_seconds)
        self.limiter = RateLimiter(
            per_minute=settings.rate_limit_per_minute,
            concurrent=settings.rate_limit_concurrent,
        )
        self.usage: UsageBuffer | None = None
        self.quota: QuotaService | None = None
        #: 上游真实费用的暂存点。见 core/costsink.py。
        self.cost_sink = CostSink()

        # Agent 运行时按 (agent_id, version) 缓存。版本不可变，所以编辑会产生新
        # 版本号、旧条目自然不再命中——不需要失效逻辑。
        self.runtimes = RuntimeCache()
        #: 构造 Agent 用的 provider。与直通层的 httpx2 客户端分开：那一层不解析
        #: 上游语义，这一层要走 pydantic-ai 的 model 抽象。
        self.provider: Any = None

        # **进程级**并发上限。
        #
        # 不能给每个 Agent 传 int：max_concurrency 的信号量是每个 Agent 实例私有的
        # （实测两个各限 1 的 Agent 全局峰值是 2，传同一个 ConcurrencyLimit 配置对象
        # 也不共享）。必须是同一个 Limiter 实例——注意是 Limiter 不是 Limit。
        from pydantic_ai.concurrency import ConcurrencyLimiter

        self.concurrency = ConcurrencyLimiter(settings.max_concurrency, name="xingcha")


async def load_upstream(state: AppState) -> None:
    """从 setting 表读上游配置并装配客户端。

    缺 key **不**拒绝启动：首次部署必然缺，那时管理员还没机会填。调用时才报
    :class:`UpstreamNotConfigured`，消息里写清楚下一步做什么。
    """
    assert state.keyring is not None
    async with state.sessionmaker() as session:
        # 环境变量只在首次启动时一次性导入，之后永久忽略
        if await setting_svc.import_env_once(
            session, state.keyring, state.settings.openrouter_api_key
        ):
            await session.commit()

        api_key = await setting_svc.get(session, state.keyring, C.SETTING_KEY_OPENROUTER_API_KEY)
        base_url = await setting_svc.get(session, state.keyring, C.SETTING_KEY_OPENROUTER_BASE_URL)

    if not api_key:
        await state.upstream.set_config(None)
        state.provider = None
        state.runtimes.clear()
        log.warning(
            "尚未配置 OpenRouter key，/v1 调用会返回明确提示。"
            "配置方式：xingcha config set openrouter.api_key -"
        )
        return

    cfg = UpstreamConfig(
        api_key=api_key,
        base_url=base_url or C.OPENROUTER_DEFAULT_BASE_URL,
        app_url=state.settings.public_url,
    )
    await state.upstream.set_config(cfg)

    # provider 换了，缓存里那些 Agent 还指着旧的 client——必须整体丢弃。
    # 这是 RuntimeCache.clear() 唯一该被调用的地方。
    from .core.builder import make_provider

    state.provider = make_provider(
        cfg, timeout=state.settings.request_timeout, cost_sink=state.cost_sink
    )
    state.runtimes.clear()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    state: AppState = app.state.xc
    settings = state.settings

    # 拼错的环境变量。不致命，但必须被看见——你以为设了某个值，实际跑的是默认值。
    warn_unknown_env()

    # umask → 数据目录 → 迁移（内含备份）→ 密钥环。顺序见 bootstrap.prepare。
    # serve 与 CLI 共用同一份，避免两处规则不一致。
    state.keyring = prepare(settings)
    log.info("密钥环就绪（%d 把密钥）", len(state.keyring))

    engine = make_engine(settings.db_path)
    state.engine = engine
    state.sessionmaker = make_sessionmaker(engine)

    # WAL 断言。静默降级会变成零星的 database is locked——最难查的一类问题。
    await assert_wal(engine)

    await load_upstream(state)

    # 预热模型目录。**这不只是为了首次 /v1/models 快一点**：目录同时是主价源，
    # 不预热的话，一个只调 chat/completions、从不列模型的调用方（也就是绝大多数
    # 业务代码）产生的每一条记录都会是 cost_source=unknown —— 等于没有账单数据。
    # 尽力而为：上游不可达时不阻塞启动，TTL 到期后会自己再试。
    up_cfg = state.upstream.config
    if up_cfg is not None:
        await state.catalog.refresh(state.upstream.client(), up_cfg.api_key)

    state.usage = UsageBuffer(state.sessionmaker)
    state.usage.start()

    # 配额的计数在内存里，启动时从数据库把当前窗口的已用量读回来播种。
    # 不播种的话每次重启配额都会归零——而重启就是这个项目的升级方式。
    state.quota = QuotaService(state.sessionmaker)
    await state.quota.reload()

    log.info("星槎 %s 已就绪 · 监听 %s:%s", __version__, settings.host, settings.port)
    try:
        yield
    finally:
        # 用量缓冲必须在这里落盘。「批量 flush」+「重启即升级」如果没有这一步，
        # 每次升级都会静默丢掉内存里那批 run 行——账单恰好在最需要它可信的时刻少报，
        # 而且丢多少无法事后察觉。这是准入项 A9。
        if state.usage is not None:
            await state.usage.close()
        await state.upstream.aclose()
        await engine.dispose()
        log.info("星槎已停止")


async def _not_configured_handler(request: Request, exc: Exception) -> JSONResponse:
    """还没配上游 key。

    503 而不是 500：这不是缺陷，是一个待办的配置步骤，而且它是可恢复的。
    消息里直接给出下一步的命令——首次部署时看到这条的人正需要它。
    """
    return JSONResponse(
        status_code=503,
        content={
            "error": {
                "message": str(exc),
                "type": "upstream_not_configured",
                "code": "upstream_not_configured",
                "param": None,
            }
        },
    )


async def _denied_handler(request: Request, exc: Exception) -> Response:
    """后台的拒绝。未登录跳登录页，其余给一个能看懂的 HTML。

    不走 /v1 的错误契约——那是给 SDK 分支用的 JSON，而这里的读者是浏览器前的人。
    """
    assert isinstance(exc, web_routes.Denied)
    if exc.status == 401:
        return RedirectResponse("/admin/login", status_code=303)
    return web_routes.security_headers(
        HTMLResponse(
            f"<!doctype html><meta charset=utf-8>"
            f"<title>操作被拒绝</title>"
            f"<link rel=stylesheet href=/admin/static/style.css>"
            f"<div class=auth-shell><div class=auth-card>"
            f"<div class=auth-title>操作被拒绝</div>"
            f"<p class='muted small mt4'>{exc.message}</p>"
            f"<a class='btn mt4' href='/admin'>返回后台</a></div></div>",
            status_code=exc.status,
        )
    )


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
        # 是一个安全关键闭集（见 tests/test_app_startup.py）。
        swagger_ui_oauth2_redirect_url=None,
    )
    app.state.xc = AppState(settings)

    app.add_exception_handler(XingchaError, xingcha_error_handler)
    app.add_exception_handler(UpstreamNotConfigured, _not_configured_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)

    _mount_probes(app)
    app.include_router(v1_api.build_router())
    web_routes.mount(app)
    app.add_exception_handler(web_routes.Denied, _denied_handler)

    # 必须在路由之前归一化 /v1 路径。见 api/normalize.py：不做这一步，
    # GET /v1/models/ 会静默落进 catch-all 被反代出去。
    app.add_middleware(NormalizeV1Path)
    return app


def _mount_probes(app: FastAPI) -> None:
    """探针与版本协商。三个都**免鉴权**——这是一个安全关键的闭集。

    往这里加路由必须同步更新 tests/test_app_startup.py 的免鉴权白名单断言。
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

        checks["upstream_configured"] = state.upstream.configured
        checks["catalog_models"] = len(state.catalog.all())
        checks["catalog_stale"] = state.catalog.is_stale
        checks["agent_runtimes_cached"] = len(state.runtimes)
        checks["cost_sink_pending"] = len(state.cost_sink)
        if state.quota is not None:
            checks["quota_rules"] = state.quota.rule_count
        if state.usage is not None:
            checks["usage_pending"] = state.usage.pending

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
        features = set(C.FEATURES)
        # 直通配额是**打开之后**才公布的：契约把"直通不执行配额"冻结了，
        # 打开它是一次收紧，调用方需要能探测到这件事。
        if app.state.xc.settings.quota_on_passthrough:
            features.add(C.FEATURE_QUOTA_PASSTHROUGH)
        return {
            "name": "xingcha",
            "version": __version__,
            "contract": C.CONTRACT_VERSION,
            "features": sorted(features),
        }
