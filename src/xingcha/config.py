"""配置的单一入口。

全部来自环境变量或 ``data/`` 目录，**不读配置文件**——C3 要求三条命令上手，中间不插入
任何配置文件编辑。

注意这里**没有** OpenRouter API key：它由管理员在 Web 上填写、Fernet 加密后存进
``setting`` 表。放进环境变量会让"Web 表单配置"这条主线断掉，而且环境变量会进
``docker inspect`` 与 ``/proc/<pid>/environ``。唯一的例外是 :attr:`Settings.openrouter_api_key`
——它只在**首次启动**时一次性导入 DB 并告警，之后永久忽略（见 services/setting.py）。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from . import contract as C

log = logging.getLogger(__name__)

ENV_PREFIX = "XINGCHA_"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- 数据 ---
    data_dir: Path = Path("./data")

    # --- 监听 ---
    #: **默认只监听本地。** 这个默认值本身是契约的一部分（开发计划 §3.11）：
    #: 改成 0.0.0.0 视为破坏性变更。生产用 Caddy 前置，xingcha 容器不映射宿主端口。
    host: str = "127.0.0.1"
    port: int = Field(default=8720, ge=1, le=65535)

    #: 对外的公开地址，用于生成回调/文档里的示例 URL。留空则用 host:port。
    public_url: str | None = None

    # --- 上游 ---
    #: 仅用于**首次启动**时把 key 导入 DB，之后永久忽略。长期配置在设置页里改。
    openrouter_api_key: str | None = None

    # --- 运行护栏 ---
    #: 必须 ≥1：传 0 会让 pydantic-ai 在建 Agent 时抛 UserError（实测），
    #: 那是一个发生在请求路径上、消息完全看不出根因的 500。
    max_concurrency: int = Field(default=16, ge=1)

    #: 单次上游请求的超时（秒）。长思考模型需要放宽。
    #: per-Agent 覆盖走 ``model_settings={'timeout': ...}``。
    request_timeout: float = Field(default=600.0, gt=0)

    #: 整轮墙钟上限（秒）。``Agent.run`` 没有 timeout 参数，只能靠 ``asyncio.timeout``。
    run_timeout: float = Field(default=900.0, gt=0)

    #: 每个 token 的速率限制。直通路径与 Agent 路径**共用**这一套。
    rate_limit_per_minute: int = Field(default=120, ge=1)
    rate_limit_concurrent: int = Field(default=8, ge=1)

    #: 是否给**直通路径**也执行配额。**默认关。**
    #:
    #: 契约把"直通不执行配额"冻结了，打开它是一次收紧。做成显式开关而不是默认打开：
    #: 那样它是部署者的决定，而不是升级的副作用；打开后 /version 的 features 会
    #: 多一项，调用方能探测到。
    quota_on_passthrough: bool = False

    # --- Web ---
    #: 会话有效期（小时）。
    session_ttl_hours: int = Field(default=24 * 7, ge=1)

    #: CORS 允许的 origin，逗号分隔。**默认空 = 不发任何 CORS 头。**
    #: 放开 origin 是纯加法，所以默认可以最严。
    cors_origins: str = ""

    # --- 模型目录 ---
    #: ``GET /v1/models`` 是否混入上游模型。
    models_include_upstream: bool = True
    #: catalog 缓存的 TTL（秒）。过期且刷新失败时走 stale-while-error。
    catalog_ttl_seconds: int = Field(default=3600, ge=60)

    # --- 可观测 ---
    log_level: str = "INFO"

    # ---------------------------------------------------------------- paths
    @property
    def db_path(self) -> Path:
        return self.data_dir / C.DB_FILENAME

    @property
    def secret_path(self) -> Path:
        return self.data_dir / C.SECRET_FILENAME

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / C.BACKUP_DIRNAME

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def ensure_data_dir(self) -> None:
        """建好数据目录并收紧权限。

        共享 VPS 上 0755 的数据目录 + 0644 的库文件，等于把 token hash 与 Fernet 密文
        交给任意本地账号。
        """
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        for d in (self.data_dir, self.backup_dir):
            try:
                d.chmod(C.DIR_MODE)
            except OSError as e:  # 只读挂载或非属主，警告而非致命
                log.warning("无法收紧 %s 的权限（%s）。请手动 chmod %o", d, e, C.DIR_MODE)


#: 已知的配置项名（不含前缀），用于识别拼错的环境变量。
_KNOWN_ENV_NAMES = {f"{ENV_PREFIX}{name.upper()}" for name in Settings.model_fields}


def warn_unknown_env() -> list[str]:
    """对拼错的 ``XINGCHA_*`` 环境变量告警，但**不**让启动失败。

    失败太严厉——一个手滑的变量名不该让线上服务起不来；但静默忽略更糟：
    你以为设了 ``XINGCHA_MAX_CONCURENCY=4``（少一个 R），实际跑的是默认值 16，
    而且没有任何迹象。
    """
    unknown = sorted(
        k for k in os.environ if k.startswith(ENV_PREFIX) and k not in _KNOWN_ENV_NAMES
    )
    for k in unknown:
        log.warning("未知的配置项 %s 被忽略（拼错了？）", k)
    return unknown


_settings: Settings | None = None


def get_settings() -> Settings:
    """进程级单例。测试里用 :func:`reset_settings` 清掉。"""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    global _settings
    _settings = None
