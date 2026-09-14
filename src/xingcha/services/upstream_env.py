"""从环境变量里发现可切换的上游。

星槎的硬规则是「上游 key 不走环境变量」（会进 ``docker inspect`` 与
``/proc/<pid>/environ``）。这一层只放宽"发现"，不放宽"存放"：环境变量只作为候选来源
列出来，真正选中的那把仍然加密落库、运行时读的是库，页面只显示变量名与脱敏值。

容器里扫不到宿主的环境变量（实测：Docker 不继承宿主环境）。所以 ``xingcha serve``
裸跑扫得到，``docker compose`` 扫不到，除非在 compose 里逐个透传。这是 Docker 的隔离
语义而不是缺陷，但页面必须说出来，否则本机看到一排、上线发现空的会被当成功能坏了。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from .. import contract as C
from ..config import Settings


def mask(value: str) -> str:
    """脱敏展示。保留前缀与末四位：前缀能认出厂商，末四位够你核对是不是那一把。全遮
    就分不出"我配的那把"和"另一把"，不遮就是把 key 印在页面上。
    """
    if not value:
        return ""
    if len(value) <= 12:
        return value[:2] + "…"
    return f"{value[:9]}…{value[-4:]}"


@dataclass(frozen=True)
class EnvUpstream:
    """环境里发现的一个候选上游。"""

    #: 环境变量名，**保留用户写的原始大小写**（页面上要照原样显示，
    #: 否则用户在 .env 里搜不到）。
    env_name: str
    #: 给人看的厂商名，如 ``DEEPSEEK``。
    label: str
    #: 已知厂商的端点；未知则 None，需要管理员填。
    base_url: str | None
    #: 脱敏后的 key。
    masked: str
    #: 这个上游有没有 /models 端点。没有 → 目录为空 → 费用与判档退化。
    has_catalog: bool


def discover(environ: Mapping[str, str] | None = None) -> list[EnvUpstream]:
    """扫出环境里所有已知厂商的上游 key。

    只认内置名单（``contract.UPSTREAM_ENV_CANDIDATES``），不做 ``*_API_KEY`` 的模式
    兜底——把 ``GITHUB_TOKEN`` 一类列进"候选上游"比少列几个危险得多。大小写不敏感。
    """
    env = os.environ if environ is None else environ
    out: list[EnvUpstream] = []
    # 按**厂商**去重，不是按变量名：一个厂商常有多个变量名
    # （DEEPINFRA_API_KEY / DEEPINFRA_TOKEN、GEMINI_ / GOOGLE_），同一家列两次
    # 只是噪音，而用户还得猜它们有什么区别。以 base_url 作为厂商身份。
    seen: set[str] = set()
    for name, value in env.items():
        upper = name.upper()
        if not value or not value.strip():
            continue
        if not C.is_known_upstream_env(upper):
            continue
        vendor = C.base_url_for_env(upper) or upper
        if vendor in seen:
            continue
        seen.add(vendor)
        out.append(
            EnvUpstream(
                env_name=name,
                label=C.vendor_label(upper),
                base_url=C.base_url_for_env(upper),
                masked=mask(value.strip()),
                has_catalog=C.has_catalog(upper),
            )
        )
    return sorted(out, key=lambda u: u.label)


def read_key(env_name: str, environ: Mapping[str, str] | None = None) -> str | None:
    """按名字取回环境变量的明文 key。大小写不敏感。只在"确认切换"那一步调用。"""
    env = os.environ if environ is None else environ
    if env_name in env and env[env_name].strip():
        return env[env_name].strip()
    upper = env_name.upper()
    for k, v in env.items():
        if k.upper() == upper and v.strip():
            return v.strip()
    return None


def default_from_env(
    environ: Mapping[str, str] | None = None,
) -> tuple[str | None, str | None]:
    """星槎自己那一对默认变量（``XINGCHA_API_KEY`` / ``XINGCHA_BASE_URL``）。

    返回 ``(key, base_url)``，按别名与大小写兼容解析。只看进程环境——文件里那一份归
    :func:`default_pair`。
    """
    env = os.environ if environ is None else environ

    def first(aliases: tuple[str, ...]) -> str | None:
        for alias in aliases:
            got = read_key(alias, env)
            if got:
                return got
        return None

    return first(C.ENV_API_KEY_ALIASES), first(C.ENV_BASE_URL_ALIASES)


def default_pair(
    settings: Settings,
    environ: Mapping[str, str] | None = None,
) -> tuple[str | None, str | None]:
    """默认那一对的唯一出口：进程环境优先，``.env`` 文件（``Settings``）兜底。

    两条部署路径把 ``.env`` 送到进程的方式不同：docker 靠 compose 的 ``env_file:`` 注进
    容器环境；uv 直跑时只有 pydantic 读了那个文件，环境里一个字都没有
    （:func:`config.load_vendor_keys` 只补内置厂商名单里的名字）。不兜底的话上游页那行
    「.env 里的默认」只在 docker 下出得来，uv 那条路上切到别家就回不来。

    兜底取 ``Settings``：pydantic 在两条路上都读同一份 ``.env``，结果必然一致。存放规则
    不变，这里只负责"发现"。
    """
    key, base = default_from_env(environ)
    if key and base:
        return key, base
    return key or settings.api_key, base or settings.base_url


# =============================================================================
# 切换前的探测
# =============================================================================


@dataclass(frozen=True)
class BrokenAgent:
    slug: str
    model: str


@dataclass(frozen=True)
class SwitchProbe:
    """切换到某个上游之前的体检结果。

    必须先探测：切上游会打断所有现有 Agent——Agent 里写死的模型名在新上游可能不存在，
    切完之后每次调用都是一个上游 4xx，而用户只会看到"Agent 突然坏了"。

    模型目录同时是判档与定价的主价源，切换后必须重拉；拉不通就根本不该切。
    """

    #: 你要切到哪个。可能是环境变量名，也可能是用户自己起的供应商名。
    ref: str
    #: ``env`` 还是 ``saved``。**必须一路带到确认那一步**——探测用一个来源、
    #: 确认用另一个的话，切过去的会是另一把 key，而两边页面看起来完全一样。
    source: str
    label: str
    base_url: str
    #: 用新 key 拉到的模型数。0 表示目录为空。
    model_count: int
    #: 目录拉取是否成功。**False 时禁止切换。**
    catalog_ok: bool
    #: 这个上游本来就没有 /models 端点——目录空是预期的，不是故障。
    catalog_expected: bool
    #: 切过去之后模型不存在的 Agent。
    broken: tuple[BrokenAgent, ...]
    #: 拉取失败时的原因（已脱敏）。
    error: str = ""

    @property
    def can_switch(self) -> bool:
        """拉不通就不让切。目录本来就没有的上游（Perplexity）例外。"""
        return self.catalog_ok or self.catalog_expected


async def probe_switch(
    *,
    ref: str,
    source: str = "env",
    base_url: str,
    api_key: str,
    agent_models: dict[str, str],
    timeout: float = 20.0,
) -> SwitchProbe:
    """用新 key 拉一次目录，并算出哪些 Agent 会失效。只读，不改任何状态。

    ``agent_models`` 是 ``{slug: model_id}``，由调用方从库里查好传进来——这一层不碰数据
    库。``ref`` 只用于回显"你要切到哪个"，可能是环境变量名也可能是用户起的供应商名。
    """
    from ..core.models_catalog import ModelsCatalog
    from ..core.upstream import UpstreamConfig, make_client

    upper = ref.upper()
    # 已知厂商用它的规范名；自己添加的就用用户起的那个名字（vendor_label 认不出来时
    # 会回落成变量名本身，那对手填的供应商恰好就是它的名字）。
    label = C.vendor_label(upper) if C.is_known_upstream_env(upper) else ref
    expected_empty = C.is_known_upstream_env(upper) and not C.has_catalog(upper)

    cfg = UpstreamConfig(api_key=api_key, base_url=base_url)
    catalog = ModelsCatalog(ttl_seconds=60)
    client = make_client(cfg, timeout=timeout)
    ok, error = False, ""
    try:
        ok = await catalog.refresh(client, cfg.api_key)
        if not ok:
            # **refresh 失败时是返回 False，不是抛异常**（它要保留旧快照）。
            # 只捕获异常的话这里拿到的是空字符串，页面只能说"目录为空"——
            # 而真实原因往往是 404（地址少了或多了 /v1），那句话才是能照着改的。
            from ..foundation.errors import redact

            error = redact(catalog.last_error or "")[:200]
    except Exception as e:  # 网络/证书/协议，什么都可能
        from ..foundation.errors import redact

        error = redact(f"{type(e).__name__}: {e}")[:200]
    finally:
        await client.aclose()

    known = {info.id for info in catalog.all()}
    broken: list[BrokenAgent] = []
    if ok and known:
        # 只在真的拿到目录时才判"模型不存在"。目录为空时说不出这句话——
        # 那会把每个 Agent 都报成坏的，而真相是"我们不知道"。
        for slug, model in sorted(agent_models.items()):
            bare = model.split(":", 1)[1] if model.startswith("openrouter:") else model
            if bare not in known:
                broken.append(BrokenAgent(slug=slug, model=bare))

    return SwitchProbe(
        ref=ref,
        source=source,
        label=label,
        base_url=base_url,
        model_count=len(known),
        catalog_ok=ok,
        catalog_expected=expected_empty,
        broken=tuple(broken),
        error=error,
    )
