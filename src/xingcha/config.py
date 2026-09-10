"""配置的单一入口。

全部来自环境变量或 ``data/`` 目录，**不读配置文件**——C3 要求三条命令上手，中间不插入
任何配置文件编辑。

注意这里**没有** OpenRouter API key：它由管理员在 Web 上填写、Fernet 加密后存进
``setting`` 表。放进环境变量会让"Web 表单配置"这条主线断掉，而且环境变量会进
``docker inspect`` 与 ``/proc/<pid>/environ``。唯一的例外是 :attr:`Settings.api_key`
——它只在**首次启动**时一次性导入 DB 并告警，之后永久忽略（见 services/setting.py）。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from . import contract as C

log = logging.getLogger(__name__)

ENV_PREFIX = "XINGCHA_"


class StartupRefused(RuntimeError):
    """启动前置条件不满足。**故意让进程起不来**，而不是带病运行。

    定义在这里而不是 db/engine.py：数据目录不可写这类失败发生在建引擎之前，
    而 config 是唯一比它更早、又只依赖 contract 的层。db/engine 从这里导出，
    对既有调用方无感。
    """


class DataDirNotWritable(StartupRefused):
    """数据目录建不出来或写不进去。

    单独一个类型是为了让 CLI 与容器日志能给出**可以直接粘贴执行**的修复命令——
    默认的 ``PermissionError: /data/backups`` 离根因太远，没人会从那句话想到
    "去宿主上 chown"。
    """


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

    #: 信任哪些反向代理发来的 ``X-Forwarded-*``。**默认谁都不信。**
    #:
    #: 为什么必须显式配：``X-Forwarded-Proto`` 决定了会话 cookie 要不要带
    #: ``Secure``（见 web/routes.py 的 cookie_secure）。无条件信任它，任何能直连
    #: 应用的人都可以伪造一个 ``X-Forwarded-Proto: https``——那本身危害有限，但
    #: 同一个头也会影响日志里记录的来源 IP 与将来的重定向拼接，所以默认不信。
    #:
    #: 不配的话，放在 HTTPS 反代后面会出现一个很隐蔽的后果：应用以为自己在 http 上，
    #: 于是 cookie 不带 Secure——功能完全正常，只是少了一层保护，没人会注意到。
    #:
    #: 取值是 uvicorn 的 ``forwarded_allow_ips``：逗号分隔的 IP/网段，或 ``*``。
    #: 只有在"应用零宿主端口、唯一入口就是那个反代"时 ``*`` 才是合理的——
    #: 共享网关那套（deploy/linux/docker-compose.gateway.yml）正是这种情形。
    trusted_proxies: str | None = None

    # --- 上游 ---
    #: 默认上游。仅用于**首次启动**时导入 DB，之后由管理面/CLI 接管。
    #:
    #: 用通用名 ``XINGCHA_API_KEY`` / ``XINGCHA_BASE_URL`` 而不是把厂商名写进变量名：
    #: 上游是可切换的，名字里带 OPENROUTER 会在切到别家之后变成谎言。
    #:
    #: ``validation_alias`` 保留旧名 ``XINGCHA_OPENROUTER_API_KEY``——改配置项名是
    #: 破坏性变更，而"升级对用户无感"是这个项目的头号承诺。两个都设时新名优先。
    api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("XINGCHA_API_KEY", "XINGCHA_OPENROUTER_API_KEY"),
    )
    base_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("XINGCHA_BASE_URL", "XINGCHA_OPENROUTER_BASE_URL"),
    )

    #: 后台密码。**设了它就以它为准**，库里的密码哈希不再参与登录。
    #:
    #: ------------------------------------------------------------------------
    #: 这一项与项目其余部分的规则不同，是有意的
    #: ------------------------------------------------------------------------
    #:
    #: 上游 key 与 Langfuse 凭据都**刻意不走环境变量**（会进 ``docker inspect`` 与
    #: ``/proc/<pid>/environ``）。后台密码比它们更敏感——它守着其余一切。
    #:
    #: 但它解决一个真问题：密码只存哈希、取不回来，一次重置或一次换实例就丢了，
    #: 而单用户自托管没有第二个管理员能帮你找回。把它交给 ``.env`` 意味着"这台机器
    #: 的文件系统已经是我的信任边界"——对自托管来说这个前提通常成立。
    #:
    #: 安全上仍然守住三件事（见 web/routes.login 与 services/websession）：
    #: 登录限流照旧生效、页面绝不回显、低于长度下限一律拒用（而不是降级放过）。
    admin_password: str | None = None

    # --- 运行护栏 ---
    #: 必须 ≥1：传 0 会让 pydantic-ai 在建 Agent 时抛 UserError（实测），
    #: 那是一个发生在请求路径上、消息完全看不出根因的 500。
    max_concurrency: int = Field(default=16, ge=1)

    #: 单次上游请求的超时（秒）。长思考模型需要放宽。
    #: per-Agent 覆盖走 ``model_settings={'timeout': ...}``。
    request_timeout: float = Field(default=600.0, gt=0)

    #: 整轮墙钟上限（秒）。``Agent.run`` 没有 timeout 参数，只能靠 ``asyncio.timeout``。
    run_timeout: float = Field(default=900.0, gt=0)

    #: 真流式的 delta 合并窗口（秒）。``None`` = 不合并，逐 token 发帧。
    #:
    #: 不合并的话一次长回答会产生上千个 SSE 帧，每帧都是一次 JSON 序列化加一次
    #: ``send``——在 1GB 的机器上这是实打实的开销，而人眼分不出 0.05s 的差别。
    stream_debounce_seconds: float | None = Field(default=0.05, gt=0)

    #: 每个 token 的速率限制。直通路径与 Agent 路径**共用**这一套。
    rate_limit_per_minute: int = Field(default=120, ge=1)
    rate_limit_concurrent: int = Field(default=8, ge=1)

    #: 是否给**直通路径**也执行配额。**默认关。**
    #:
    #: 契约把"直通不执行配额"冻结了，打开它是一次收紧。做成显式开关而不是默认打开：
    #: 那样它是部署者的决定，而不是升级的副作用；打开后 /version 的 features 会
    #: 多一项，调用方能探测到。
    quota_on_passthrough: bool = False

    # --- 可观测 ---
    #: trace 里是否包含提示词与模型输出。
    #:
    #: **打开意味着这些文本会离开这台机器。** 自建 Langfuse 关起门来看，那正是要的；
    #: 指向别人家的托管服务，就要先想清楚。默认 True 是因为不含内容的 trace 基本
    #: 回答不了"提示词改了一版为什么变差"——那是开 trace 的主要理由。
    trace_include_content: bool = True

    #: 上报到 OTLP 端点的服务名。多环境（staging / prod）共用一个 Langfuse 项目时
    #: 靠它区分。
    trace_service_name: str = "xingcha"

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
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.backup_dir.mkdir(parents=True, exist_ok=True)
        except PermissionError as e:
            # bind mount 最常见的失败方式，而默认报错离根因很远：
            # 容器里以 UID 10001 运行，镜像里 chown 过的 /data 被宿主目录整个盖掉，
            # 于是看到的是 "Permission denied: /data/backups"——没人会从这句话想到
            # "去宿主上 chown"。所以这里直接把要敲的命令写出来。
            raise DataDirNotWritable(
                f"数据目录不可写：{e.filename or self.data_dir}\n"
                f"容器内以 UID {C.CONTAINER_UID} 运行，宿主上的挂载目录必须属于它。\n"
                f"在宿主上执行："
                f"sudo chown -R {C.CONTAINER_UID}:{C.CONTAINER_UID} <你的 data 目录>\n"
                "（非容器部署则是：确认当前用户对该目录有写权限）"
            ) from e
        for d in (self.data_dir, self.backup_dir):
            try:
                d.chmod(C.DIR_MODE)
            except OSError as e:  # 只读挂载或非属主，警告而非致命
                log.warning("无法收紧 %s 的权限（%s）。请手动 chmod %o", d, e, C.DIR_MODE)


def _known_env_names() -> set[str]:
    """所有**合法**的 ``XINGCHA_*`` 变量名。

    三个来源，少任何一个都会产生假警报：

    1. 字段名本身；
    2. 字段上的 ``AliasChoices``——``XINGCHA_OPENROUTER_API_KEY`` 是 v0 留下的
       兼容名，只看字段名的话它会被判成拼错；
    3. :data:`contract.ORCHESTRATION_ENV_NAMES`——编排层的变量（端口、绑定地址、
       挂载点）。它们不是应用设置，但 ``env_file`` 会把整份 ``.env`` 注进容器。

    假警报比漏报更伤：用户配得完全正确却被告知拼错，于是学会忽略这类警告，
    而**真的拼错时也就没人看了**。
    """
    names = {f"{ENV_PREFIX}{name.upper()}" for name in Settings.model_fields}
    for field in Settings.model_fields.values():
        alias = field.validation_alias
        if isinstance(alias, AliasChoices):
            names.update(str(a) for a in alias.choices if isinstance(a, str))
        elif isinstance(alias, str):
            names.add(alias)
    return names | set(C.ORCHESTRATION_ENV_NAMES)


#: 已知的配置项名，用于识别拼错的环境变量。
_KNOWN_ENV_NAMES = _known_env_names()


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
