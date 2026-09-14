"""配置的单一入口。

全部来自环境变量或 ``data/`` 目录，不读配置文件——三条命令上手，中间不插入任何配置
文件编辑。

这里没有 OpenRouter API key：它由管理员在 Web 上填写、Fernet 加密后存进 ``setting``
表（环境变量会进 ``docker inspect`` 与 ``/proc/<pid>/environ``）。唯一的例外是
:attr:`Settings.api_key`，只在首次启动时一次性导入 DB 并告警，之后永久忽略。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Final

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from . import contract as C

log = logging.getLogger(__name__)

ENV_PREFIX = "XINGCHA_"


class StartupRefused(RuntimeError):
    """启动前置条件不满足。故意让进程起不来，而不是带病运行。

    定义在这里而不是 db/engine.py：数据目录不可写发生在建引擎之前，而 config 是唯一
    比它更早、又只依赖 contract 的层。db/engine 从这里导出，对既有调用方无感。
    """


class DataDirNotWritable(StartupRefused):
    """数据目录建不出来或写不进去。

    单独一个类型，是为了让 CLI 与容器日志给出可直接粘贴的修复命令——默认的
    ``PermissionError: /data/backups`` 离"去宿主上 chown"太远。
    """


#: ``.env`` 的路径。与 :class:`Settings` 的 ``env_file`` 是同一份，写两遍会漂。
ENV_FILE: Final = Path(".env")


def load_vendor_keys(env_file: Path | None = None) -> list[str]:
    """把 ``.env`` 里的厂商 key 补进 ``os.environ``，返回补了哪几个。

    pydantic-settings 只把 ``XINGCHA_`` 前缀的项读进 :class:`Settings`，不会把文件里
    其它行注进 ``os.environ``；而上游页扫厂商 key 走的正是 ``os.environ``。所以
    「各厂商的 key 写 .env，后台上游页会扫出来」这句话只在 docker 下成立（compose 的
    ``env_file:`` 会注整份文件）。走 ``xc.bat`` / ``uv run`` 时文件里的 key 到不了进程，
    页面上要么空的、要么扫到 shell 里那把同名的旧 key——后者最难查：看起来配好了，
    点切换却 401。

    两条纪律：只补内置名单里的名字（``contract.is_known_upstream_env``），否则一个
    配置文件就能改任意环境变量（``PATH``、``PYTHONPATH`` 都在射程内）；已经在环境里的
    不覆盖，真实环境变量优先于文件。
    """
    path = env_file or ENV_FILE
    if not path.exists():
        return []

    loaded: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name, value = name.strip(), value.strip().strip("'\"")
        if not value or not C.is_known_upstream_env(name.upper()):
            continue
        if os.environ.get(name.upper()):
            continue  # 环境里已经有了，不覆盖
        os.environ[name.upper()] = value
        loaded.append(name.upper())
    return loaded


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
    #: **默认只监听本地。** 这个默认值本身是契约的一部分（README 「对外契约」§9 运行护栏）：
    #: 改成 0.0.0.0 视为破坏性变更。生产用 Caddy 前置，xingcha 容器不映射宿主端口。
    host: str = "127.0.0.1"
    port: int = Field(default=8720, ge=1, le=65535)

    #: 对外的公开地址，用于生成回调/文档里的示例 URL。留空则用 host:port。
    public_url: str | None = None

    #: 信任哪些反向代理发来的 ``X-Forwarded-*``。默认谁都不信，要信就显式写。
    #:
    #: 两个方向的错都是静默的：``X-Forwarded-Proto`` 决定会话 cookie 带不带 ``Secure``
    #: （见 web/admin/security.py），无条件信任的话谁都能伪造一个 https、还污染日志里的
    #: 来源 IP；配漏了则是挂在 HTTPS 反代后面却以为自己在 http 上，cookie 少一层保护而
    #: 功能完全正常。
    #:
    #: 取值是 uvicorn 的 ``forwarded_allow_ips``（逗号分隔的 IP/网段，或 ``*``）。只有
    #: "应用只绑回环、唯一入口是那个反代"时 ``*`` 才合理——挂网关那套正是这种情形。
    trusted_proxies: str | None = None

    #: 允许把后台嵌进 iframe 的来源，逗号分隔，形如 ``http://10.20.1.13:30800``。
    #: 默认空 = 谁都不许嵌（``frame-ancestors 'none'`` + ``X-Frame-Options: DENY``）。
    #:
    #: 存在的理由：后台被挂进别的门户当一个页签时，上面两个头会让 iframe 渲染成一块
    #: 空白，而浏览器控制台之外没有任何提示。门户那边于是往往去反代里把这两个头剥掉，
    #: 等于把防点击劫持整个抹掉、还抹在我们看不见的地方。给一个显式名单，让这件事回到
    #: 被嵌的这一侧，且只放行指名的来源。
    #:
    #: 配了之后三件事一起变（见 web/admin/security.py）：CSP 的 ``frame-ancestors`` 换成
    #: 这份名单、``X-Frame-Options`` 不再发送（它没有"只允许某个源"的合法写法）、同源
    #: 校验把这些来源当作同站放行。
    #:
    #: 写完整的源：scheme + host + 端口（非默认端口必须写）。末尾斜杠会被去掉——浏览器
    #: 发的 ``Origin`` 永远不带它，多一个斜杠就是永远匹配不上。
    admin_embed_origins: str | None = None

    @property
    def embed_origins(self) -> tuple[str, ...]:
        """:attr:`admin_embed_origins` 解析成规范化的元组。"""
        raw = self.admin_embed_origins or ""
        return tuple(item.strip().rstrip("/") for item in raw.split(",") if item.strip())

    # --- 上游 ---
    #: 默认上游。仅用于首次启动时导入 DB，之后由管理面/CLI 接管。用通用名而不是把厂商
    #: 名写进变量名：上游可切换，带 OPENROUTER 会在切到别家之后变成谎言。
    #:
    #: ``validation_alias`` 保留旧名 ``XINGCHA_OPENROUTER_API_KEY``——改配置项名是破坏性
    #: 变更。两个都设时新名优先。
    api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("XINGCHA_API_KEY", "XINGCHA_OPENROUTER_API_KEY"),
    )
    base_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("XINGCHA_BASE_URL", "XINGCHA_OPENROUTER_BASE_URL"),
    )

    #: 后台密码。设了它就以它为准，库里的密码哈希不再参与登录。
    #:
    #: 与项目其余部分的规则不同，是有意的：上游 key 与 Langfuse 凭据都刻意不走环境变量，
    #: 而后台密码比它们更敏感。但它解决一个真问题——密码只存哈希、取不回来，一次重置或
    #: 换实例就丢了，而单用户自托管没有第二个管理员能帮你找回。交给 ``.env`` 意味着
    #: "这台机器的文件系统已经是我的信任边界"，对自托管通常成立。
    #:
    #: 安全上仍守住两件事（见 web/admin/login.py 与 services/websession）：登录限流照旧
    #: 生效、页面绝不回显。短于长度下限照用不拒，理由见
    #: :func:`services.websession.env_password_usable`。
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

    #: 真流式的 delta 合并窗口（秒）。``None`` = 不合并，逐 token 发帧。不合并的话一次
    #: 长回答会产生上千个 SSE 帧（每帧一次 JSON 序列化加一次 ``send``），而人眼分不出
    #: 0.05s 的差别。
    stream_debounce_seconds: float | None = Field(default=0.05, gt=0)

    #: 每个 token 的速率限制。直通路径与 Agent 路径**共用**这一套。
    rate_limit_per_minute: int = Field(default=120, ge=1)
    rate_limit_concurrent: int = Field(default=8, ge=1)

    #: 是否给直通路径也执行配额。默认关：契约把"直通不执行配额"冻结了，打开它是一次
    #: 收紧，做成显式开关才是部署者的决定而不是升级的副作用。打开后 /version 的
    #: features 会多一项，调用方能探测到。
    quota_on_passthrough: bool = False

    # --- 可观测 ---
    #: trace 里是否包含提示词与模型输出。打开意味着这些文本会离开这台机器——自建
    #: Langfuse 正是要的，指向别人家的托管服务就要先想清楚。默认 True 是因为不含内容的
    #: trace 回答不了"提示词改了一版为什么变差"，而那是开 trace 的主要理由。
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

    # --- 日志 ---
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
        """建好数据目录并收紧权限：共享 VPS 上 0755 目录 + 0644 库文件，等于把 token
        hash 与 Fernet 密文交给任意本地账号。
        """
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.backup_dir.mkdir(parents=True, exist_ok=True)
        except PermissionError as e:
            # bind mount 最常见的失败方式：镜像里 chown 过的 /data 被宿主目录整个盖掉，
            # 报错只说 "Permission denied: /data/backups"。直接把要敲的命令写出来。
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
    """所有合法的 ``XINGCHA_*`` 变量名。三个来源，少一个就产生假警报：

    1. 字段名本身；
    2. 字段上的 ``AliasChoices``——兼容名只看字段名的话会被判成拼错；
    3. :data:`contract.ORCHESTRATION_ENV_NAMES`——编排层的端口/绑定地址/挂载点，不是
       应用设置，但 ``env_file`` 会把整份 ``.env`` 注进容器。

    假警报比漏报更伤：用户学会忽略这类警告之后，真的拼错时也就没人看了。
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
    """对拼错的 ``XINGCHA_*`` 环境变量告警，但不让启动失败。

    一个手滑的变量名不该让线上服务起不来；但静默忽略更糟——你以为设了
    ``XINGCHA_MAX_CONCURENCY=4``（少一个 R），实际跑的是默认值，且没有任何迹象。
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
