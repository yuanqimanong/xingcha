"""从数据库行构造 Agent。

**这是上游版本适配的唯一集中点。** ``AgentSpec`` 的字段与 ``CAPABILITY_TYPES``
会随 pydantic-ai 演进，所有兼容处理只写在这个文件里；别处不解释 spec 字段的含义
（开发计划 §6 标准 3）。升级 pydantic-ai 时只需要改这里。

下面每一条注释里的"实测"都是真跑过的，不是从文档抄的——文档在这几处是错的。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

import httpx2
from openai import AsyncOpenAI
from pydantic import ValidationError
from pydantic_ai import Agent, AgentSpec, UsageLimits
from pydantic_ai.exceptions import UserError
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.providers.openrouter import OpenRouterProvider

from .. import contract as C
from ..contract import Tier
from ..errors import AgentBuildFailed, AgentSpecInvalid
from .costsink import CostSink, make_hook
from .guarantee import GuaranteeCounters, attach_validator, limits_for, output_spec
from .upstream import UpstreamConfig, attribution_headers

log = logging.getLogger(__name__)

#: 两种 provider 的公共类型。用哪一种由 base_url 决定，见 :func:`make_provider`。
Provider = OpenRouterProvider | OpenAIProvider


# =============================================================================
# spec 校验（保存时）
# =============================================================================


def _spec_schema() -> dict[str, Any]:
    """官方给出的 AgentSpec JSON Schema。"""
    return AgentSpec.model_json_schema_with_capabilities(custom_capability_types())


def validate_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """保存前校验 spec，返回规范化后的 dict。

    **必须显式跑一遍官方 schema 的 jsonschema 校验。**

    ``AgentSpec`` 是 ``extra='ignore'``：拼错的字段会被**静默吞掉**，
    ``from_spec({"totally_bogus": 1})`` 照样构造成功。而官方生成的 schema 是
    ``additionalProperties: false``。两者不一致，意味着靠 ``from_spec`` 本身
    探测不到字段拼错或上游改名——静默降级会一路跑到线上。

    决策 2 那句"整块存 JSON，升级只改 builder 一个文件"，要靠这一步才成立。
    """
    import jsonschema

    try:
        jsonschema.Draft202012Validator(_spec_schema()).validate(spec)
    except jsonschema.ValidationError as e:
        where = "/".join(map(str, e.absolute_path)) or "根"
        raise AgentSpecInvalid(f"Agent 定义不合法（{where}）：{e.message}") from e

    try:
        parsed = AgentSpec.model_validate(spec)
    except ValidationError as e:
        first = e.errors()[0]
        where = "/".join(map(str, first["loc"])) or "根"
        raise AgentSpecInvalid(f"Agent 定义不合法（{where}）：{first['msg']}") from e

    # by_alias 不可省：json_schema_path 的 alias 是 `$schema` 且未开
    # populate_by_name，写全名会被静默丢弃，round-trip 会丢字段。
    out = parsed.model_dump(by_alias=True, exclude_none=True)
    return runnable_capabilities(out)


def runnable_capabilities(spec: dict[str, Any]) -> dict[str, Any]:
    """把 capability 改回 ``from_spec`` **收得下**的形状。

    **上游自己的 round-trip 不自洽**（实测 pydantic-ai 2.35.3）：

    * ``AgentSpec.model_dump()`` 把 ``["Thinking"]`` 规范化成 ``[{"name": "Thinking"}]``；
    * 而 ``Agent.from_spec()`` **拒绝**那个形状，报
      ``Capability 'name' is not in the provided custom_capability_types``
      ——它把整个 dict 当成"能力名叫 name"。

    星槎存的正是 dump 出来的那一份，于是：**保存成功，每次调用都 500。** 任何勾了
    能力的 Agent 都建不起来，包括「可观测」那个勾（它就是 ``Instrumentation``
    能力）。导出的 ``agent.yaml`` 同样带着这个坏形状。

    ``from_spec`` 收 ``"Thinking"`` 与 ``{"Thinking": {args}}``；官方 schema 只认
    ``"Thinking"`` 与 ``{"name": "Thinking"}``。**两个集合的交集只有裸字符串**，
    所以这里一律降回字符串。带参数的能力现在没有表单入口；将来有了，得同时绕过
    schema 校验与 from_spec 的这条分歧，那时候再说，别现在假装支持。

    在 :func:`validate_spec`（写入）与 :func:`build`（读取）两处都做：前者修新存
    的与导出的，后者让库里已有的坏行不需要迁移就能跑。幂等。
    """
    caps = spec.get("capabilities")
    if not isinstance(caps, list):
        return spec
    fixed: list[Any] = []
    for item in caps:
        if isinstance(item, dict) and isinstance(item.get("name"), str) and len(item) == 1:
            fixed.append(item["name"])
        else:
            fixed.append(item)
    if fixed != caps:
        spec = {**spec, "capabilities": fixed}
    return spec


def custom_capability_types() -> tuple[type, ...]:
    """自定义 capability。v0.2 为空——逃生舱在后续版本。

    注意上游对这类类有三条硬约束（实测）：必须继承 ``AbstractCapability``、
    必须**自己**被 ``@dataclass`` 装饰（继承来的不算）、``get_serialization_name()``
    不能返回 None。``Capability`` 基类显式返回 None，所以直接继承它会报
    "has opted out of serialization"。
    """
    return ()


def declarable_capabilities() -> list[str]:
    """当前 pydantic-ai 版本支持在 spec 里声明的能力名。

    运行时读取而不是硬编码：官方新增能力会自动出现在表单里。
    """
    from pydantic_ai.capabilities import CAPABILITY_TYPES

    return sorted(CAPABILITY_TYPES)


def capability_params_schema() -> dict[str, Any]:
    """每个 capability 的参数 schema，供表单生成字段。

    **不能用 ``inspect.signature`` 或 ``dataclasses.fields``**（实测）：
    有 4 个 capability 覆写了 ``from_spec`` 且签名与 ``__init__`` 不同——
    ``PrefixTools`` 的 ``__init__`` 参数叫 ``wrapped``、spec 里叫 ``capability``，
    照 ``__init__`` 生成表单 100% 报错；``dataclasses.fields`` 还会把有默认值的
    参数报成必填并暴露私有字段。唯一正确的来源是官方 schema 的 ``$defs``。
    """
    defs = _spec_schema().get("$defs", {})
    return {
        name.removeprefix("spec_params_"): body
        for name, body in defs.items()
        if name.startswith("spec_params_")
    }


# =============================================================================
# 上游 model
# =============================================================================


def is_openrouter(base_url: str) -> bool:
    """这个上游是不是 OpenRouter 本体。

    按**主机名**判断，不看路径：中转会把路径改成各种样子，但域名不会假装是
    openrouter.ai。判错的代价是不对称的——见 :func:`make_provider`。
    """
    from urllib.parse import urlparse

    host = (urlparse(base_url).hostname or "").lower()
    return host == "openrouter.ai" or host.endswith(".openrouter.ai")


def make_provider(
    cfg: UpstreamConfig, *, timeout: float, cost_sink: CostSink | None = None
) -> Provider:
    """构造 provider。

    **不是 OpenRouter 就不能用 ``OpenRouterProvider``。**

    它的 ``model_profile()`` 在模型名里没有 ``/`` 时**直接抛 UserError**
    （"model names must be prefixed with the upstream provider"）。而厂商直连
    与大多数中转的模型 id 恰恰是裸的（``deepseek-v4-flash``）——于是：

    * ``GET /v1/models`` 正常（那只是一次 HTTP 拉取），
    * 直通正常（原样转发），
    * **只有 Agent 挂**，而且是一个 500 "服务内部错误"。

    也就是说，产品的核心卖点在任何非 OpenRouter 上游上都不可用，而三条路径里
    唯一坏掉的那条报的是一句看不出原因的话。实测踩到。

    反过来判错是安全的：``OpenAIProvider`` 只是少了几条按厂商前缀挑 profile 的
    提示，不会硬失败。所以这里按域名严格识别 OpenRouter，其余一律走通用的那个。

    走自建 ``AsyncOpenAI`` 而不是让 provider 自己建，因为 ``OpenRouterProvider``
    **不接受 base_url**（实测：签名里没有，也没有任何别名），而大陆中转恰恰必须改它。

    三个参数每一个不设都会咬人：

    ``max_retries=0``
        SDK 默认重试 2 次。实测 timeout=0.3 时墙钟被放大到 2.17 秒，并且**把中转
        打了三遍**。重试只该有一层，交给 pydantic-ai 的 retries / guarantee。

    ``trust_env=False``
        httpx2 默认 True，会读机器的 ALL_PROXY。socks5 下客户端在**构造阶段**就
        ImportError（socksio 未装），服务起不来且报错看不出跟代理有关。

    手写的 attribution headers
        传了 ``openai_client=`` 之后，官方**不再**注入 HTTP-Referer / X-Title
        （那段注入只在它自建 client 的分支里）。所以这不是重复代码，删掉会让
        OpenRouter 后台看不到来源。
    """
    hooks: dict[str, list[Any]] = {}
    if cost_sink is not None:
        # 上游真实费用只能在 HTTP 层拿到：pydantic-ai 填的 usage.cost 是 genai-prices
        # 的估价，上游 body 里那个真实值被 isinstance(v, int) 过滤掉了（实测差 400 倍）。
        hooks["response"] = [make_hook(cost_sink)]

    http = httpx2.AsyncClient(
        trust_env=False,
        timeout=httpx2.Timeout(timeout, connect=min(15.0, timeout)),
        event_hooks=hooks or None,
    )
    client = AsyncOpenAI(
        base_url=cfg.normalized_base(),
        api_key=cfg.api_key,
        max_retries=0,
        http_client=http,
        default_headers=attribution_headers(cfg) or None,
    )
    if is_openrouter(cfg.base_url):
        return OpenRouterProvider(openai_client=client)
    return OpenAIProvider(openai_client=client)


def enable_instrumentation(tracing: Any) -> None:
    """装配 pydantic-ai 的埋点，但**默认不开**。

    这是本项目唯一调用 pydantic-ai 埋点 API 的地方（架构标准 3：上游适配点唯一）。

    ------------------------------------------------------------------------
    为什么是"按 Agent 开"而不是全局开
    ------------------------------------------------------------------------

    埋点做的事是把每次模型请求的**完整消息与响应**记成 span 属性，然后发到外部
    地址。这件事的答案在不同 Agent 之间通常不同：一个跑客户合同的 Agent 与一个
    跑内部分类的 Agent，对"对话内容能不能离开这台机器"的回答不该被一个全局开关
    统一。

    ``instrument_all(settings)`` 设的是**默认值**——它只作用于没有单独声明
    ``Instrumentation`` 能力的 Agent。所以这里传 ``False``：默认谁都不上报，
    想上报的 Agent 在 spec 里声明那个 capability。

    ``instrument_all`` 仍然要调（而不是完全不调）：pydantic-ai 需要一个
    tracer_provider 才知道往哪儿发，而那是全局基础设施——**地址是全局的，
    开关是按 Agent 的**。
    """
    from pydantic_ai import Agent
    from pydantic_ai.models.instrumented import InstrumentationSettings

    if tracing is None:
        Agent.instrument_all(False)
        return
    Agent.instrument_all(
        InstrumentationSettings(
            tracer_provider=tracing.provider,
            include_content=tracing.include_content,
            include_binary_content=False,  # 图片/音频进 span 会把体积撑爆
        )
    )


#: 表单要暴露的模型参数，**按官方 ModelSettings 的字段名**。
#:
#: 只列这几个：它们是调模型时真会动的旋钮。其余（``extra_body`` / ``logit_bias`` /
#: ``extra_headers`` / ``tool_choice``）要么是逃生舱、要么形状复杂到表单放不下——
#: 那些走「导出 bundle 手改 agent.yaml 再 apply」这条路。
#:
#: 每一项都在 :func:`model_settings_fields` 里对着官方 schema 校验过存在，所以
#: pydantic-ai 哪天改了字段名，构建期就会红，而不是在某次调用时静默失效。
FORM_MODEL_SETTINGS: Final[tuple[tuple[str, str, str], ...]] = (
    # (字段名, 中文标签, 提示)
    ("temperature", "temperature", "0 最确定、越高越发散。抽取类任务通常设 0。"),
    ("top_p", "top_p", "核采样。与 temperature 二选一调，同时调难以推理。"),
    ("max_tokens", "max_tokens", "单次回复的上限。设太小会让长回答被截断。"),
    ("seed", "seed", "同样输入尽量给同样输出。多数上游只是尽力而为，不保证。"),
    ("presence_penalty", "presence_penalty", "抑制重复出现的话题。范围 -2 ~ 2。"),
    ("frequency_penalty", "frequency_penalty", "抑制重复用词。范围 -2 ~ 2。"),
    ("top_k", "top_k", "只从概率最高的 k 个词里采样。部分上游不支持。"),
    ("timeout", "timeout（秒）", "**单次**上游请求的超时，不是整轮。长思考模型要放宽。"),
)

#: 能力清单里对**单用户自托管**真正有用、且不需要额外参数的那些。
#:
#: 全列 14 个只会让表单变成一份 pydantic-ai 内部术语表——``PrefixTools`` /
#: ``SetToolMetadata`` / ``IncludeToolReturnSchemas`` 是给框架使用者调工具协议的，
#: 在这个后台里勾了也没有可观察的效果。
#:
#: ``Instrumentation`` 不在这里：它由「可观测」分区单独呈现（语义完全不同——那是
#: "把这个 Agent 的对话发到外部"，与"给模型加个能力"不该并列在同一个勾选框列表里）。
#:
#: MCP 也不在这里：它需要服务器地址与鉴权，得先有一个配置页。
#: 每一条的说明都是**实测**出来的，不是照官方清单抄的。五个勾看起来等价，
#: 实际可用性差得很远，而失败全都发生在调用那一刻、不在保存那一刻：
#:
#: * ``Thinking`` —— 纯参数，任何上游都收。实测 DeepSeek 直连与 OpenRouter 都通。
#: * ``WebSearch`` —— 要两个条件同时成立：provider 侧的
#:   ``openai_chat_supports_web_search``（**OpenRouter 全放行、厂商直连全不放行**），
#:   以及模型自己支持 ``WebSearchTool``。实测 glm-5.3-flash 经 OpenRouter 可用，
#:   同一个能力经 DeepSeek 直连直接被拒。
#: * ``WebFetch`` / ``ImageGeneration`` —— 要模型支持对应的原生工具，支持面很窄
#:   （实测 glm-5.3-flash 两个都不支持，它只支持 WebSearchTool）。
#: * ``ToolSearch`` —— 纯本地、不需要上游点头，但它做的是"工具很多时先检索再调用"，
#:   而星槎现在**一个工具都注册不了**（唯一入口是 MCP，还没接）。所以它现在是空转。
#:
#: 上游还提供 ``local=`` 回退（``WebSearch(local='duckduckgo')`` / ``WebFetch(local=True)``），
#: 那会让抓取发生在**星槎自己的进程里**。没有开放：那等于给服务端开一个由模型
#: 决定目标地址的出网原语，也就是 SSRF；要开得先过 urlguard，而那是一次单独的决定。
#: **这条通道原生只认一个工具。** 实测 pydantic-ai 2.35.3：
#:
#:     class OpenAIChatModel:
#:         def supported_native_tools(cls): return frozenset({WebSearchTool})
#:
#: 星槎对所有模型都用 ``OpenAIChatModel``（见 make_model 的注释：``OpenRouterModel``
#: 对缺 ``provider`` 字段的中转响应会硬失败，而走中转正是这个项目的用途）。所以
#: 网页抓取、图像生成、原生 MCP **跟模型无关，一律做不到**——它们只存在于
#: ``OpenAIResponsesModel``（OpenAI 的 Responses API）那条通道上。
#:
#: 这不是"支持的模型很少"，是**零**。两者的区别很要紧：前者让人去换模型试，后者
#: 让人知道该等哪条路通。
CHAT_CHANNEL_NATIVE_TOOLS: Final = ("WebSearch",)

#: ``(能力名, 标签, 说明, 勾上时带的参数)``。
#:
#: 第四项是这一版新加的：勾选框此前只能表达"开/不开"，而 ``local=`` 这类参数恰恰
#: 是让一个能力从"永远报错"变成"能用"的东西。spec 里带参数的形状是
#: ``{"名字": {参数}}``，:func:`validate_spec` 会规范化成
#: ``{"name": ..., "arguments": {...}}``，而 ``from_spec`` **收这一种**（不收
#: 只有 name 一个键的那种，见 runnable_capabilities）。
FORM_CAPABILITIES: Final[tuple[tuple[str, str, str, dict[str, Any] | None], ...]] = (
    (
        "Thinking",
        "思考",
        "让模型先想再答。任何上游都收，但只有推理型模型真的会想，且会多花 token。"
        "实测在 DeepSeek 直连与 OpenRouter 上都可用。",
        None,
    ),
    (
        "WebSearch",
        "联网搜索",
        "**由上游去搜**，不占这台机器的网络。这条通道上唯一能交给上游做的能力。"
        "要两个条件：上游得是 OpenRouter 这一类（厂商直连一律被拒），模型自己也要支持。"
        "拿不准用下面的「试运行」跑一次，一次就知道。",
        None,
    ),
    (
        "WebFetch",
        "网页抓取",
        "**由星槎自己的进程去抓**，不是上游——网页抓取在这条 API 通道上跟模型无关地"
        "做不到，只能本地做。于是能抓到的范围就是**这台机器能到的范围**："
        "国内站点可以，被墙的站点不行。私有网段与云元数据地址一律拒（上游自带守卫）。",
        {"local": True},
    ),
    (
        "ToolSearch",
        "工具搜索",
        "工具很多时让模型先检索再调用。**现在开了等于没开**：星槎还没有注册工具的"
        "入口（唯一的路是 MCP，未接），没有工具可检索。不报错，但也不做任何事。",
        None,
    ),
    (
        "ImageGeneration",
        "图像生成",
        "**这条通道上做不到。** 勾了必然报错——原生要 OpenAI 的 Responses API"
        "（星槎有意没走，中转会挂），本地回退要传一个 Python 函数，网页表单表达不了。"
        "留在这里是为了别让已经勾过的 Agent 静默丢设置。",
        None,
    ),
)

#: 可观测那一项对应的 capability 名。单独拎出来，见 FORM_CAPABILITIES 的说明。
CAPABILITY_INSTRUMENTATION: Final = "Instrumentation"


def model_settings_fields() -> tuple[tuple[str, str, str], ...]:
    """表单要用的模型参数，**对着官方 schema 校验过**。

    不校验的话，pydantic-ai 改字段名之后表单会静默失效：填了 temperature、
    存进 spec、``extra='ignore'`` 把它吞掉——你以为设了、实际跑的是默认值。
    """
    known = set(_spec_schema()["$defs"]["ModelSettings"].get("properties", {}))
    missing = [name for name, _, _ in FORM_MODEL_SETTINGS if name not in known]
    if missing:  # pragma: no cover - 只在上游改名时触发
        raise RuntimeError(
            f"这些字段在官方 ModelSettings 里不存在了：{missing}。"
            f"pydantic-ai 改了字段名，FORM_MODEL_SETTINGS 要跟着改。"
        )
    return FORM_MODEL_SETTINGS


def capabilities_from_form(raw: Any) -> list[Any]:
    """勾选框 → spec 里的 capabilities 列表。

    没参数的写成裸字符串，有参数的写成 ``{"名字": {参数}}``——这两种正好是
    :func:`validate_spec` 的官方 schema 与 ``from_spec`` **同时**接受的形状
    （交集，见 :func:`runnable_capabilities`）。

    参数不是可选的花活：网页抓取只有带上 ``local=True`` 才可能工作，不带就是一个
    勾了必然报错的开关。
    """
    out: list[Any] = []
    for name, _, _, args in form_capabilities():
        if raw.get(f"cap_{name}"):
            out.append({name: dict(args)} if args else name)
    return out


def form_capabilities() -> tuple[tuple[str, str, str, dict[str, Any] | None], ...]:
    """表单要用的能力，**对着官方 CAPABILITY_TYPES 校验过**。"""
    known = set(declarable_capabilities())
    missing = [name for name, _, _, _ in FORM_CAPABILITIES if name not in known]
    if missing:  # pragma: no cover
        raise RuntimeError(f"这些能力在 pydantic-ai 里不存在了：{missing}")
    if CAPABILITY_INSTRUMENTATION not in known:  # pragma: no cover
        raise RuntimeError("Instrumentation 能力不见了，可观测的按 Agent 开关无从实现")
    return FORM_CAPABILITIES


def _strip_prefix(model: str) -> str:
    """表单可能存成 ``openrouter:openai/gpt-5``；provider 已显式给出，去掉前缀。"""
    return model.split(":", 1)[1] if model.startswith("openrouter:") else model


def native_ok(model_id: str, provider: Provider, *, catalog_says: bool) -> bool:
    """这个模型**真的**能走原生 JSON Schema 约束吗（T1 / T1+ 的前提）。

    ------------------------------------------------------------------------
    必须问两个人，而且要取交集
    ------------------------------------------------------------------------

    此前只问模型目录。而真正的闸在 pydantic-ai 里，**在本地、发请求之前**就会拦：

        if params.output_mode == 'native' and not profile.get('supports_json_schema_output', False):
            raise UserError('Native structured output is not supported by this model.')

    两个来源各自错一个方向（实测）：

    * 目录说 yes、profile 说 no —— ``z-ai/glm-5.3-flash``、``qwen/qwen3.8-flash``
      在 OpenRouter 目录里都标着 ``structured_outputs: true``。判档因此保住 T1、
      **保存时不给任何降级提示**，然后每一次调用都失败。这一条最糟：管理员以为
      自己拿到了最强的形状保证，实际拿到的是一个必然报错的 Agent。
    * 目录说 no、profile 说 yes —— 厂商直连时目录里往往连能力字段都没有
      （DeepSeek 的 ``/models`` 只回 id/object/owned_by），而通用 profile 对没
      见过的名字给的是默认值。

    取交集在两个方向上都安全：错判成"不支持"只是降级到 T2、多花点重试成本，
    而错判成"支持"是对用户**谎称有保证**。这与 ``resolve_tier`` 里那句"未知模型
    一律当作不支持"是同一条原则。
    """
    if not catalog_says:
        return False
    try:
        profile = make_model(model_id, provider).profile
    except Exception:  # pragma: no cover - 模型名不被 provider 接受，那是另一条错误路径
        return False
    return bool(profile.get("supports_json_schema_output", False))


def make_model(model_id: str, provider: Provider) -> OpenAIChatModel:
    """构造 model。

    **用 ``OpenAIChatModel`` 而不是 ``OpenRouterModel``。**

    后者会带上 OpenRouter 的 prompt-cache 处理，看起来更"对口"，但它对响应缺
    ``provider`` 字段会**硬失败**——而中转（New API 一类）不保证回传那个字段。
    星槎的核心用途就是走中转，所以这里选稳。

    代价是拿不到上游的 prompt-cache 计价优化，但那只影响费用**预估精度**，
    而费用主价源已经改成模型目录的单价，影响被补偿掉了。

    别好心改回 OpenRouterModel。`xingcha doctor` 里有一条体检项会告诉你上游到底
    带不带 provider 字段。
    """
    return OpenAIChatModel(_strip_prefix(model_id), provider=provider)


# =============================================================================
# 构造结果
# =============================================================================


# =============================================================================
# 提示词组装（用户模板 + 少样本）
# =============================================================================

#: 用户提示词模板里代表"调用方发来的那段话"的占位符。
PROMPT_PLACEHOLDER: Final = "{{input}}"

#: 星槎自己的东西放进 ``AgentSpec.metadata`` 的这个命名空间下。
#:
#: 为什么放 metadata：``AgentSpec`` 的官方 schema 是 ``additionalProperties: false``，
#: 加顶层字段会被校验直接打回；而 ``metadata`` 是官方留的自由字典。放在带命名空间
#: 的键下，将来上游或用户往 metadata 里写别的也不会撞上。
#:
#: 代价要说清楚：上游**不解释**这里的任何东西，模板与示例是星槎在运行时应用的。
#: 所以导出物里不能只把 metadata 带走了事——见 exporter，它把两者烤进 run.py。
SPEC_NS: Final = "xingcha"


@dataclass(frozen=True, slots=True)
class Example:
    """一组少样本示例：给模型看一次"这样问、该这样答"。"""

    user: str
    assistant: str


@dataclass(frozen=True, slots=True)
class Prompting:
    """系统提示词之外的两件事。

    系统提示词（``instructions``）是"你是谁、按什么规则做事"，这两件是"这一轮怎么
    问、答成什么样"——通道不同，所以不能塞进同一个框：

    * ``user_template`` 包住调用方发来的内容。没有它，"请从下面的合同里抽取信息："
      这句框架就得每个调用方自己记，而 Agent 的卖点恰恰是"提示词固定在服务端"。
    * ``examples`` 是成对的 user/assistant，以**真正的历史轮**送进去。这是
      "assistant 提示词"唯一有用的形态：结构化输出下，示例能明显压低 schema 违规，
      而每次违规就是一次重试、一次真金白银。
    """

    user_template: str = ""
    examples: tuple[Example, ...] = ()

    #: T2 把 schema 递给模型的通道，见 :data:`guarantee.OUTPUT_CHANNELS`。
    #: 和上面两项一样存在 metadata 里——它也不是 AgentSpec 的字段。
    output_channel: str = "tool"

    @property
    def is_empty(self) -> bool:
        return not self.user_template and not self.examples and self.output_channel == "tool"


def prompting_from_spec(spec: dict[str, Any]) -> Prompting:
    """spec → :class:`Prompting`。字段缺失或形状不对一律退回空，不抛。

    读取路径必须宽容：库里可能存着更早版本写的 spec，而一个老 Agent 不该因为
    metadata 里少个键就整个跑不起来。写入路径（:func:`validate_prompting`）才严格。
    """
    raw = (spec.get("metadata") or {}).get(SPEC_NS) or {}
    if not isinstance(raw, dict):
        return Prompting()
    template = raw.get("user_template")
    pairs = raw.get("examples")
    examples = []
    if isinstance(pairs, list):
        for item in pairs:
            if isinstance(item, dict) and item.get("user") and item.get("assistant"):
                examples.append(Example(str(item["user"]), str(item["assistant"])))
    channel = raw.get("output_channel")
    return Prompting(
        user_template=template if isinstance(template, str) else "",
        examples=tuple(examples),
        output_channel=channel if channel in ("tool", "prompt") else "tool",
    )


def validate_prompting(
    user_template: str, examples: list[Example], output_channel: str = "tool"
) -> Prompting:
    """保存前校验。

    模板非空却不含占位符是**必须拦下**的：那样调用方发来的内容会被整个丢掉，
    每次调用都拿同一段固定文本去问模型。表现是"Agent 好像不看我的输入"，
    而表单上一切正常——静默失败里最难查的一类。
    """
    template = user_template.strip()
    if template and PROMPT_PLACEHOLDER not in template:
        raise AgentSpecInvalid(
            f"用户提示词模板里必须有 {PROMPT_PLACEHOLDER}，用来放调用方发来的内容。"
            "没有它，调用方发什么都不会进入这次调用。"
        )
    kept = [e for e in examples if e.user.strip() and e.assistant.strip()]
    for e in examples:
        if bool(e.user.strip()) != bool(e.assistant.strip()):
            raise AgentSpecInvalid("少样本示例要成对：问和答都得填，只填一半的那组请删掉。")
    return Prompting(
        user_template=template,
        examples=tuple(Example(e.user.strip(), e.assistant.strip()) for e in kept),
        output_channel=output_channel if output_channel in ("tool", "prompt") else "tool",
    )


def prompting_to_spec(spec: dict[str, Any], prompting: Prompting) -> None:
    """把 :class:`Prompting` 写回 spec 的 metadata。空则**不写键**。

    空也写一个 ``{"xingcha": {}}`` 的话，每个 Agent 的 spec 里都多一坨没内容的
    结构，导出的 agent.yaml 也跟着脏。
    """
    if prompting.is_empty:
        return
    body: dict[str, Any] = {}
    if prompting.user_template:
        body["user_template"] = prompting.user_template
    if prompting.examples:
        body["examples"] = [{"user": e.user, "assistant": e.assistant} for e in prompting.examples]
    if prompting.output_channel != "tool":
        body["output_channel"] = prompting.output_channel
    spec.setdefault("metadata", {})[SPEC_NS] = body


@dataclass
class AgentRuntime:
    """一个可执行的 Agent 及其运行期附属物。

    按 ``(agent_id, version)`` 缓存：编辑 Agent 会产生新版本，旧条目自然淘汰，
    不需要显式失效逻辑。
    """

    agent: Agent
    tier: Tier
    schema: dict[str, Any] | None
    counters: GuaranteeCounters
    limits: UsageLimits
    model_id: str

    #: 两阶段（T1+）的第一阶段：不带任何格式约束，纯自由推理。
    #:
    #: 只有 T1+ 有这个。它存在的全部意义是让推理那一步**不受格式约束干扰**——
    #: 文献显示格式约束会削弱推理，而两阶段把这两件事分开。
    reason_agent: Agent | None = None

    #: 用户提示词模板与少样本。上游不认识它们，是星槎在组装消息时应用的。
    prompting: Prompting = Prompting()

    @property
    def is_structured(self) -> bool:
        return self.schema is not None

    @property
    def is_two_stage(self) -> bool:
        return self.reason_agent is not None


@dataclass(frozen=True, slots=True)
class BuildOptions:
    max_retries: int = 2
    max_tool_steps: int = 8
    max_tokens: int = 200_000
    max_cost_usd: Decimal | None = None


def build(
    *,
    spec_json: str | dict[str, Any],
    tier: Tier,
    out_schema: str | dict[str, Any] | None,
    provider: OpenRouterProvider,
    options: BuildOptions,
    concurrency: Any = None,
) -> AgentRuntime:
    """``agent_version`` 的一行 → 可执行的 Agent。

    ``spec_json`` 原样来自数据库，这里是唯一解释它的地方。
    """
    spec = json.loads(spec_json) if isinstance(spec_json, str) else dict(spec_json)
    # 库里可能存着 0.1 时期写下的、from_spec 收不下的 capability 形状。
    spec = runnable_capabilities(spec)
    schema = json.loads(out_schema) if isinstance(out_schema, str) else out_schema

    model_id = spec.get("model")
    if not isinstance(model_id, str) or not model_id:
        # AgentSpec 层面 model 其实是**可选**的（实测），所以 model_validate 不会拦，
        # 错误会推迟到 from_spec 抛 UserError。在这里显式拦下，报错更靠近原因。
        raise AgentSpecInvalid("Agent 定义里没有 model")

    # **make_model 要在 try 里。**
    #
    # 它会抛 UserError（模型名不被 provider 接受之类），而放在 try 外面的话那个
    # 异常一路冒到最外层，变成一句"服务内部错误，请把 run_id 给管理员"——而这恰恰
    # 是最需要说清原因的一类失败：它每次都发生，不是偶发。
    try:
        model = make_model(model_id, provider)
    except (UserError, ValueError) as e:
        raise AgentBuildFailed(
            f"构造 model {model_id!r} 失败：{type(e).__name__}: {e}", reason=str(e)
        ) from e

    kwargs: dict[str, Any] = {
        "model": model,
        "custom_capability_types": custom_capability_types(),
        "retries": options.max_retries,
    }
    if concurrency is not None:
        kwargs["max_concurrency"] = concurrency
    if schema is not None:
        # **必须显式传 output_type。**
        #
        # 只把 schema 留在 spec 里 → from_spec 设成不校验的 StructuredDict；
        # 既 pop 掉又不传 → 退化成 str，校验器收到原始 JSON 字符串，
        # 于是连完全合法的输出都会被打到重试耗尽。两种都实测过。
        kwargs["output_type"] = output_spec(
            tier,
            schema,
            max_retries=options.max_retries,
            channel=prompting_from_spec(spec).output_channel,
        )

    try:
        agent = Agent.from_spec(spec, **kwargs)
    except (ValidationError, ValueError, UserError) as e:
        # 三类都可能出现：ValidationError 来自字段类型错，ValueError 来自未知
        # capability 名，UserError 来自 model 缺失或未知模型名。
        raise AgentBuildFailed(f"{type(e).__name__}: {e}", reason=str(e)) from e

    counters = attach_validator(agent, tier, schema) if schema is not None else GuaranteeCounters()

    # 两阶段（T1+）：再造一个**不带任何输出约束**的 agent 做第一步。
    #
    # 用同一份 spec（同样的指令、同样的模型），只是不传 output_type——那正是
    # "让推理不受格式约束干扰"的字面实现。文献显示格式约束会削弱推理，两阶段
    # 把这两件事分开，代价是约两倍的调用成本。
    reason_agent: Agent | None = None
    if tier is Tier.T1P and schema is not None:
        # 要让第一阶段真的**没有**格式约束，必须从 spec 里去掉 output_schema。
        #
        # 传 output_type=str 是不够的（实测）：str 正是那个参数的默认值，
        # pydantic-ai 分不清"显式传了 str"和"根本没传"，于是照样回落到 spec 里的
        # output_schema、走 tools 通道——第一阶段仍然带着约束，两阶段就白做了。
        # 这个坑很隐蔽，因为代码读起来完全像是生效了。
        reason_spec = {k: v for k, v in spec.items() if k != "output_schema"}
        reason_kwargs = {k: v for k, v in kwargs.items() if k != "output_type"}
        try:
            reason_agent = Agent.from_spec(reason_spec, **reason_kwargs)
        except (ValidationError, ValueError, UserError) as e:
            raise AgentBuildFailed(
                f"两阶段的推理 agent 构造失败：{type(e).__name__}: {e}", reason=str(e)
            ) from e

    return AgentRuntime(
        reason_agent=reason_agent,
        agent=agent,
        tier=tier,
        schema=schema,
        counters=counters,
        limits=limits_for(
            max_retries=options.max_retries,
            max_tool_steps=options.max_tool_steps,
            max_tokens=options.max_tokens,
            max_cost_usd=options.max_cost_usd,
        ),
        model_id=_strip_prefix(model_id),
        prompting=prompting_from_spec(spec),
    )


def spec_from_form(
    *,
    name: str,
    description: str | None,
    instructions: str,
    model: str,
    capabilities: list[str] | None = None,
    model_settings: dict[str, Any] | None = None,
    retries: int | None = None,
    prompting: Prompting | None = None,
) -> dict[str, Any]:
    """表单字段 → AgentSpec dict。

    ``instrument`` **不是** AgentSpec 字段（实测），对应的是名为 ``Instrumentation``
    的 capability——所以表单的"可观测"开关要写进 capabilities，不能建顶层输入项。
    """
    spec: dict[str, Any] = {"model": model, "name": name, "instructions": instructions}
    if description:
        spec["description"] = description
    if capabilities:
        # capability 在 spec 里的形状是 `- Name` 或 `- Name: {args}`。
        # 写成 `- name: Thinking` 会被当成"能力名叫 name、参数是 Thinking"而报错。
        spec["capabilities"] = capabilities
    if model_settings:
        spec["model_settings"] = model_settings
    if retries is not None:
        # 必须是裸 int 或 {'output': n}。2.35.3 新增的 {'tools': n} **不影响**
        # output 校验重试——写成那样会让重试预算看起来设了、实际没设。
        spec["retries"] = retries
    if prompting is not None:
        prompting_to_spec(spec, prompting)
    return spec


def model_settings_from_form(raw: dict[str, str]) -> dict[str, Any]:
    """表单里的模型参数 → ``model_settings`` dict。

    **空字符串一律丢弃，不写成 0 或 null。** 表单里留空的意思是"不设这一项、用
    上游默认"，而写进 spec 的 ``temperature: 0`` 是一个截然不同的指令——把留空
    当成 0 会静默把每个 Agent 都变成确定性输出。

    类型按官方 schema 走：整数字段收 int，其余收 float。收错类型的话
    ``AgentSpec`` 的 ``extra='ignore'`` 不会报错，它会**静默丢掉**那一项。
    """
    ints = {"max_tokens", "seed", "top_k"}
    out: dict[str, Any] = {}
    for field, _, _ in model_settings_fields():
        text = (raw.get(field) or "").strip()
        if not text:
            continue
        try:
            out[field] = int(text) if field in ints else float(text)
        except ValueError as e:
            from ..errors import AgentSpecInvalid

            raise AgentSpecInvalid(f"{field} 不是合法的数字：{text!r}") from e
    return out


def capability_names(caps: list[Any]) -> set[str]:
    """从 spec 的 capabilities 里取出能力名。**三种形状都要认。**

    ``validate_spec`` 会把 ``["Thinking"]`` **规范化成**
    ``[{"name": "Thinking"}]``，而手写的 agent.yaml 里还可能是
    ``[{"Thinking": {...参数}}]``。

    只认一种的下场：反填时把 ``{"name": "Thinking"}`` 的第一个 key 当成能力名，
    于是每个 Agent 都被读成开了一个叫 ``name`` 的能力——**编辑页所有勾都是空的，
    一保存就把用户设过的能力全清掉**。实测踩过。
    """
    out: set[str] = set()
    for cap in caps:
        if isinstance(cap, str):
            out.add(cap)
        elif isinstance(cap, dict):
            # 规范形状：{"name": "Thinking", ...}；带参数形状：{"Thinking": {...}}
            named = cap.get("name")
            if isinstance(named, str) and named:
                out.add(named)
            else:
                out.update(k for k in cap if isinstance(k, str))
    return out


def form_view(spec: dict[str, Any]) -> dict[str, Any]:
    """AgentSpec → 表单要回填的值。是 :func:`spec_from_form` 的反向。

    编辑一个 Agent 时必须能看到**当前的**参数值。反填不了的话，编辑就等于重填——
    而"我只是想改一句提示词"会把之前设过的 temperature 悄悄清掉。
    """
    settings = spec.get("model_settings") or {}
    names = capability_names(spec.get("capabilities") or [])
    prompting = prompting_from_spec(spec)
    return {
        "settings": {k: settings.get(k, "") for k, _, _ in model_settings_fields()},
        "capabilities": names,
        "instrumented": CAPABILITY_INSTRUMENTATION in names,
        "user_template": prompting.user_template,
        "examples": list(prompting.examples),
        "output_channel": prompting.output_channel,
    }


#: 供 doctor 与设置页显示。
UPSTREAM_MODEL_PREFIX = "openrouter:"
CONTRACT_TIER_VALUES = tuple(t.value for t in C.Tier)
