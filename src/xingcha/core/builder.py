"""从数据库行构造 Agent。

上游版本适配的唯一集中点：``AgentSpec`` 的字段与 ``CAPABILITY_TYPES`` 会随
pydantic-ai 演进，所有兼容处理只写在这个文件里，别处不解释 spec 字段的含义。
注释里标「实测」的都是真跑过的——文档在这几处是错的。
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

from ..contract import Tier
from ..foundation.errors import AgentBuildFailed, AgentSpecInvalid
from .costsink import CostSink, make_hook
from .guarantee import GuaranteeCounters, attach_validator, limits_for, output_spec
from .upstream import UpstreamConfig, attribution_headers, new_async_client

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

    必须显式跑一遍官方 schema 的 jsonschema 校验：``AgentSpec`` 是 ``extra='ignore'``，
    拼错的字段被静默吞掉，``from_spec({"totally_bogus": 1})`` 照样成功；而官方 schema
    是 ``additionalProperties: false``。只靠 ``from_spec`` 探测不到字段拼错或上游改名，
    静默降级会一路跑到线上。
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
    """把 capability 改回 ``from_spec`` 收得下的形状。

    上游自己的 round-trip 不自洽（实测 pydantic-ai 2.35.3）：``model_dump()`` 把
    ``["Thinking"]`` 规范化成 ``[{"name": "Thinking"}]``，而 ``from_spec()`` 拒绝那个
    形状（把整个 dict 当成"能力名叫 name"）。星槎存的正是 dump 那一份，于是保存成功、
    每次调用 500，任何勾了能力的 Agent 都建不起来。

    ``from_spec`` 收 ``"Thinking"`` 与 ``{"Thinking": {args}}``，官方 schema 只认
    ``"Thinking"`` 与 ``{"name": "Thinking"}``——交集只有裸字符串，所以一律降回字符串。
    带参数的能力目前没有表单入口，别现在假装支持。

    写入（:func:`validate_spec`）与读取（:func:`build`）两处都做：前者修新存的与导出
    的，后者让库里已有的坏行不迁移也能跑。幂等。
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


#: OpenRouter 的联网开关：请求体里的 ``plugins``。不带 ``engine`` 是刻意的——留空等同
#: ``:online``，由上游自选（自带检索的走 native，其余回退 Exa）。实测同一个 grok-4.3
#: 留空注入 ~11K token / 15 条引用，写死 ``engine="exa"`` 只剩 ~2.7K / 5 条。
_WEB_SEARCH_PLUGIN: Final[dict[str, Any]] = {"id": "web"}


def _capability_name(item: Any) -> str | None:
    """capability 条目 → 名字。收 ``"X"`` / ``{"X": {...}}`` / ``{"name": "X"}`` 三种形状。"""
    if isinstance(item, str):
        return item
    if isinstance(item, dict) and len(item) == 1:
        if isinstance(item.get("name"), str):
            return item["name"]
        only = next(iter(item))
        return only if isinstance(only, str) else None
    return None


def websearch_to_plugin(spec: dict[str, Any]) -> dict[str, Any]:
    """把 ``WebSearch`` 能力翻成 OpenRouter 的 ``plugins``，并摘掉这个 capability。

    没有这一层的话，勾了「联网搜索」的 Agent 不报错也不搜索：pydantic-ai 的
    ``WebSearchTool`` 在 ``OpenAIChatModel`` 上翻成 OpenAI 自家的 ``web_search_options``，
    而 OpenRouter 不实现那个字段。实测（2026-09-13）：塞非法值照样 200，与瞎编的字段
    同待遇（``reasoning_effort="banana"`` / ``plugins=[{"id":"banana"}]`` 都会 400）；
    而本该拦住它的门禁也失效——``openai_chat_supports_web_search`` 对 grok / glm /
    gemini / gpt / deepseek 全是 True，pydantic-ai 永远不抛"not supported"。

    结果是最坏的失败形态：模型没拿到材料，照常编一个自信的答案，成品上看不出来。

    必须摘掉 capability，只加 plugins 不摘的话那个死字段照发。合并而不是覆盖：手写
    spec 的人可以在 ``model_settings.extra_body.plugins`` 里自带一条 web 插件，那条
    优先。幂等。
    """
    caps = spec.get("capabilities")
    if not isinstance(caps, list):
        return spec
    kept = [c for c in caps if _capability_name(c) != "WebSearch"]
    if len(kept) == len(caps):
        return spec

    settings = dict(spec.get("model_settings") or {})
    extra_body = dict(settings.get("extra_body") or {})
    plugins = list(extra_body.get("plugins") or [])
    if not any(isinstance(p, dict) and p.get("id") == "web" for p in plugins):
        plugins.append(dict(_WEB_SEARCH_PLUGIN))
    extra_body["plugins"] = plugins
    settings["extra_body"] = extra_body
    return {**spec, "capabilities": kept, "model_settings": settings}


def custom_capability_types() -> tuple[type, ...]:
    """自定义 capability。目前为空。

    上游对这类类有三条硬约束（实测）：必须继承 ``AbstractCapability``、必须自己被
    ``@dataclass`` 装饰（继承来的不算）、``get_serialization_name()`` 不能返回 None
    （``Capability`` 基类显式返回 None，直接继承会报 "has opted out of serialization"）。
    """
    return ()


def declarable_capabilities() -> list[str]:
    """当前 pydantic-ai 版本支持在 spec 里声明的能力名。

    运行时读取而不是硬编码：官方新增能力会自动出现在表单里。
    """
    from pydantic_ai.capabilities import CAPABILITY_TYPES

    return sorted(CAPABILITY_TYPES)


# =============================================================================
# 上游 model
# =============================================================================


def is_openrouter(base_url: str) -> bool:
    """这个上游是不是 OpenRouter 本体。

    按主机名判断，不看路径：中转会改路径，但域名不会假装是 openrouter.ai。判错的
    代价不对称，见 :func:`make_provider`。
    """
    from urllib.parse import urlparse

    host = (urlparse(base_url).hostname or "").lower()
    return host == "openrouter.ai" or host.endswith(".openrouter.ai")


def make_provider(
    cfg: UpstreamConfig, *, timeout: float, cost_sink: CostSink | None = None
) -> Provider:
    """构造 provider。

    不是 OpenRouter 就不能用 ``OpenRouterProvider``：它的 ``model_profile()`` 在模型名
    没有 ``/`` 时直接抛 UserError，而厂商直连与多数中转的 id 恰恰是裸的。症状是
    ``GET /v1/models`` 与直通都正常、只有 Agent 挂成 500「服务内部错误」。反过来判错
    是安全的（``OpenAIProvider`` 只是少了几条按厂商前缀挑 profile 的提示），所以按
    域名严格识别，其余一律走通用的那个。

    走自建 ``AsyncOpenAI`` 而不是让 provider 自己建：``OpenRouterProvider`` 不接受
    base_url，而大陆中转恰恰必须改它。三个参数每一个不设都会咬人：

    ``max_retries=0``
        SDK 默认重试 2 次。实测 timeout=0.3 时墙钟放大到 2.17 秒，并把中转打了三遍。
        重试只该有一层，交给 pydantic-ai 的 retries / guarantee。

    走 ``upstream.new_async_client``
        Agent 调模型走的就是这里，必须和拉目录 / 直通用同一个建法——读环境代理。写死
        ``trust_env=False`` 的后果是目录拉得到、体检也通，只有 Agent 调用被上游按出口
        IP 挡回 ``This model is not available in your region.``。

    手写的 attribution headers
        传了 ``openai_client=`` 之后官方不再注入 HTTP-Referer / X-Title，删掉会让
        OpenRouter 后台看不到来源。
    """
    hooks: dict[str, list[Any]] = {}
    if cost_sink is not None:
        # 上游真实费用只能在 HTTP 层拿到：pydantic-ai 填的 usage.cost 是 genai-prices
        # 的估价，上游 body 里那个真实值被 isinstance(v, int) 过滤掉了（实测差 400 倍）。
        hooks["response"] = [make_hook(cost_sink)]

    http = new_async_client(
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
    """装配 pydantic-ai 的埋点。没有上报目标时全体关闭；配了目标之后它是全局默认值。

    本项目唯一调用 pydantic-ai 埋点 API 的地方（架构标准 3：上游适配点唯一）。

    埋点把每次模型请求的完整消息与响应记成 span 属性发到外部地址，而"对话内容能不能
    离开这台机器"在不同 Agent 之间答案不同，所以按 Agent 开。

    ``instrument_all`` 设的是默认值，只作用于没有单独声明 ``Instrumentation`` 的
    Agent：没有上报目标时传 ``False``，谁都不上报；配了目标之后默认值是这份 settings，
    也就是默认全体上报。地址是全局的，开关是按 Agent 的，所以它仍然要调——pydantic-ai
    需要一个 tracer_provider 才知道往哪儿发。
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


#: 表单要暴露的模型参数，按官方 ModelSettings 的字段名。
#:
#: 只列调模型时真会动的旋钮。其余（``extra_body`` / ``logit_bias`` /
#: ``extra_headers`` / ``tool_choice``）要么是逃生舱、要么形状复杂到表单放不下，那些
#: 走「导出 bundle 手改 agent.yaml 再 apply」。每一项都在
#: :func:`model_settings_fields` 里对着官方 schema 校验过存在，改名会在构建期就红。
#:
#: 第三格是留空时实际生效的值，直接印在输入框里——调 temperature 的人想知道的正是
#: "不动它是多少"。星槎留空时压根不发这个字段，真正决定取值的是上游，所以措辞是
#: "上游默认"。``max_tokens`` / ``seed`` / ``top_k`` 没有数字可写就不写，编一个更糟。
#: ``timeout`` 是唯一星槎自己知道确切值的，占位符由 :func:`model_settings_fields` 填。
FORM_MODEL_SETTINGS: Final[tuple[tuple[str, str, str, str], ...]] = (
    # (字段名, 中文标签, 留空时实际是多少, 提示)
    ("temperature", "temperature", "上游默认 1", "0 最确定、越高越发散。抽取类任务通常设 0。"),
    ("top_p", "top_p", "上游默认 1", "核采样。与 temperature 二选一调，同时调难以推理。"),
    ("max_tokens", "max_tokens", "不限，到模型自己的上限", "设太小会让长回答被截断。"),
    ("seed", "seed", "不发送", "同样输入尽量给同样输出。多数上游只是尽力而为，不保证。"),
    ("presence_penalty", "presence_penalty", "上游默认 0", "抑制重复出现的话题。范围 -2 ~ 2。"),
    ("frequency_penalty", "frequency_penalty", "上游默认 0", "抑制重复用词。范围 -2 ~ 2。"),
    ("top_k", "top_k", "不发送", "只从概率最高的 k 个词里采样。部分上游不支持。"),
    (
        "timeout",
        "timeout（秒）",
        "{request_timeout}",
        "单次上游请求的超时，不是整轮。长思考模型要放宽。",
    ),
)

#: 厂商专属的参数。不在通用 ``ModelSettings`` 里，所以单独一张表——
#: :func:`model_settings_fields` 那道校验用的是通用那份，混进去会把构建搞红。
#:
#: ``openai_reasoning_effort`` 实测真的会发出去（落成请求体里的 ``reasoning_effort``）。
#: 必须实测：``AgentSpec`` 的 ``extra='ignore'`` 会静默吞掉收不下的键，症状是"我设了、
#: 没生效"。取值是闭集而不是数字，所以单独走 ``<select>``。
FORM_CHOICE_SETTINGS: Final[tuple[tuple[str, str, str, str, tuple[str, ...]], ...]] = (
    (
        "openai_reasoning_effort",
        "reasoning_effort",
        "上游默认 medium",
        "思考深度。只有推理型模型认它，别的模型会忽略（不报错）。越高越慢越贵。",
        ("minimal", "low", "medium", "high"),
    ),
)

#: 能力清单里对单用户自托管真正有用、且不需要额外参数的那些。
#:
#: 全列 14 个只会让表单变成一份 pydantic-ai 内部术语表：``PrefixTools`` /
#: ``SetToolMetadata`` / ``IncludeToolReturnSchemas`` / ``NativeTool`` 只在有工具时
#: 才有意义，``ReinjectSystemPrompt`` 重注的是 ``system_prompt`` 而星槎用
#: ``instructions``，勾了都没有可观察的效果。``Instrumentation`` 由「可观测」分区
#: 单独呈现（那是"把对话发到外部"，不该与"给模型加个能力"并列）；MCP 需要服务器
#: 地址与鉴权，得先有一个配置页。
#:
#: 这条通道原生只认一个工具。实测 pydantic-ai 2.35.3 的
#: ``OpenAIChatModel.supported_native_tools()`` 只返回 ``{WebSearchTool}``，而星槎对
#: 所有模型都用 ``OpenAIChatModel``（见 make_model）。直接打这道闸测过三个模型
#: （含 gpt-5），``WebFetchTool`` / ``ImageGenerationTool`` / ``MCPServerTool`` 一律
#: ``not supported by this model``——不是支持的模型少，是与模型无关地为零，它们只
#: 存在于 ``OpenAIResponsesModel`` 那条通道上。
#:
#: 所以这里只放真的能用的：``Thinking`` 纯本地不碰这道闸；``WebSearch`` 是唯一能交给
#: 上游做的，要 provider 侧的 ``openai_chat_supports_web_search``（OpenRouter 全放行、
#: 厂商直连全不放行）与模型自身支持同时成立。
#:
#: 拿掉的三个：``WebFetch`` 原生做不到，本地回退等于开一个由模型决定目标地址的出网
#: 原语（SSRF），要开得先过 urlguard；``ImageGeneration`` 的本地回退要传一个 Python
#: 函数，网页表单表达不了；``ToolSearch`` 做的是"工具很多时先检索再调用"，而星槎还
#: 没有注册工具的入口，不报错也不做事。
FORM_CAPABILITIES: Final[tuple[tuple[str, str, str, dict[str, Any] | None], ...]] = (
    (
        "Thinking",
        "深度思考",
        "让模型先想再答。只有推理型模型会真的思考，其余模型勾了也不改变行为；"
        "思考过程本身要花 token。",
        None,
    ),
    (
        "WebSearch",
        "联网搜索",
        "由上游完成检索，把材料附进上下文，不占这台机器的网络。检索到的材料"
        "按 prompt token 计费，一次调用可能多花上万 token，不需要时别勾。",
        None,
    ),
)

#: 可观测那一项对应的 capability 名。单独拎出来，见 FORM_CAPABILITIES 的说明。
CAPABILITY_INSTRUMENTATION: Final = "Instrumentation"


def model_settings_fields(
    request_timeout: float | None = None,
) -> tuple[tuple[str, str, str, str], ...]:
    """表单要用的模型参数，对着官方 schema 校验过。

    不校验的话，pydantic-ai 改字段名之后表单会静默失效：填了 temperature、存进 spec、
    ``extra='ignore'`` 把它吞掉，你以为设了、实际跑的是默认值。

    ``request_timeout`` 用来把 ``timeout`` 那项的"留空是多少"填成真值；不传就留占位符。
    """
    known = set(_spec_schema()["$defs"]["ModelSettings"].get("properties", {}))
    missing = [name for name, _, _, _ in FORM_MODEL_SETTINGS if name not in known]
    if missing:  # pragma: no cover - 只在上游改名时触发
        raise RuntimeError(
            f"这些字段在官方 ModelSettings 里不存在了：{missing}。"
            f"pydantic-ai 改了字段名，FORM_MODEL_SETTINGS 要跟着改。"
        )
    if request_timeout is None:
        return FORM_MODEL_SETTINGS
    shown = f"{request_timeout:g}"
    return tuple(
        (f, label, default.format(request_timeout=shown), hint)
        for f, label, default, hint in FORM_MODEL_SETTINGS
    )


def sampling_params_ignored(model_id: str, provider: Any) -> bool:
    """这个模型会不会把 ``temperature`` / ``top_p`` **静默丢掉**。

    pydantic-ai 把所有 ``openai/`` 前缀的模型名都当成"推理模型且思考常开"
    （profile 里 ``thinking_always_enabled``），而推理模型不接受采样参数，于是它们在
    发请求前被剥掉——只在服务端日志里 ``warnings.warn`` 一句。对着页面的人来说症状是
    "我设了 temperature=0，跑出来还是发散的"，而表单上一切正常。

    所以这里问一次 profile，把答案摆到表单上。**只影响 ``openai/*``**：
    ``anthropic/*``、``google/*``、裸模型名都照常发送。

    这是在读第三方的内部结构，所以整段包在 try 里：读不到就说"不知道"（返回 False），
    宁可少一句提示，也不能因为上游换了个数据形状就让整个表单 500。
    """
    if not model_id or provider is None:
        return False
    try:
        profile = provider.model_profile(model_id)
        if profile is None:
            return False
        if isinstance(profile, dict):
            return bool(profile.get("thinking_always_enabled"))
        return bool(getattr(profile, "thinking_always_enabled", False))
    except Exception:  # pragma: no cover - 只在 pydantic-ai 换形状时走到
        log.debug("取不到 %s 的 model profile，不显示采样参数提示", model_id, exc_info=True)
        return False


def choice_settings_fields() -> tuple[tuple[str, str, str, str, tuple[str, ...]], ...]:
    """厂商专属的枚举参数。字段名对着 ``OpenAIChatModelSettings`` 校验。

    校验的理由与上面同一条：收不下的键会被静默吞掉，而症状是"设了没生效"。
    """
    from pydantic_ai.models.openai import OpenAIChatModelSettings

    known = set(OpenAIChatModelSettings.__annotations__)
    missing = [name for name, *_ in FORM_CHOICE_SETTINGS if name not in known]
    if missing:  # pragma: no cover - 只在上游改名时触发
        raise RuntimeError(
            f"这些字段在 OpenAIChatModelSettings 里不存在了：{missing}。"
            f"pydantic-ai 改了字段名，FORM_CHOICE_SETTINGS 要跟着改。"
        )
    return FORM_CHOICE_SETTINGS


def capabilities_from_form(raw: Any) -> list[Any]:
    """勾选框 → spec 里的 capabilities 列表。

    没参数的写成裸字符串，有参数的写成 ``{"名字": {参数}}``——这两种正好是官方 schema
    与 ``from_spec`` 同时接受的形状（见 :func:`runnable_capabilities`）。当前两项都
    不带参数，带参数那一支留给将来（``WebFetch(local=True)`` 就是那种）。
    """
    #: 扫 ``cap_*`` 前缀而不是只遍历 FORM_CAPABILITIES：只遍历当前清单的话，一个早先
    #: 勾过 ImageGeneration 的 Agent 下次保存就被悄悄清掉了。扫前缀 + 对着官方全集
    #: 校验，页面才能把这类"已不再提供但你确实设过"的能力渲染出来让人决定去留。
    offered = {name: args for name, _, _, args in form_capabilities()}
    known = set(declarable_capabilities())
    out: list[Any] = []
    for key in raw:
        if not key.startswith("cap_") or not raw.get(key):
            continue
        name = key[len("cap_") :]
        if name not in known:
            continue
        args = offered.get(name)
        out.append({name: dict(args)} if args else name)
    return sorted(out, key=lambda c: next(iter(c)) if isinstance(c, dict) else c)


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
    """这个模型真的能走原生 JSON Schema 约束吗（T1 / T1+ 的前提）。

    必须问模型目录与 pydantic-ai 的 profile 两边并取交集。真正的闸在 pydantic-ai 里、
    在发请求之前：``output_mode == 'native'`` 而 profile 的
    ``supports_json_schema_output`` 为假时直接 ``UserError``。

    两个来源各自错一个方向（实测）：目录说 yes、profile 说 no（``z-ai/glm-5.3-flash``、
    ``qwen/qwen3.8-flash`` 目录里都标着 ``structured_outputs: true``）会保住 T1、保存时
    不给降级提示，然后每次调用都失败；目录说 no、profile 说 yes 则出现在厂商直连——
    目录里连能力字段都没有，而通用 profile 对没见过的名字给默认值。

    取交集两个方向都安全：错判成"不支持"只是降到 T2 多花点重试，错判成"支持"是对用户
    谎称有保证。与 ``resolve_tier`` 的"未知模型一律当作不支持"同一条原则。
    """
    if not catalog_says:
        return False
    try:
        profile = make_model(model_id, provider).profile
    except Exception:  # pragma: no cover - 模型名不被 provider 接受，那是另一条错误路径
        return False
    return bool(profile.get("supports_json_schema_output", False))


@dataclass(frozen=True, slots=True)
class CapabilityCheck:
    """一条"这个模型能不能干这个"的结论。

    ``state`` 三态而不是布尔：厂商直连的 ``/models`` 常常只回 id，那时候对着一个明明
    会推理的模型打叉是在撒谎——"声明了不支持"与"没有信息"必须分开。
    """

    key: str
    label: str
    state: str  # yes | no | unknown
    detail: str


def model_report(model_id: str, provider: Provider, info: Any) -> list[CapabilityCheck]:
    """选定模型之后，这个模型到底能干什么。

    这一份是给**保存之前**看的。此前所有这类判定都只在调用那一刻生效：判档降级
    在保存后才提示，能力不支持要等第一次真调用才报错，T2 的通道选错了同样如此。
    而这些信息**在选完模型的那一刻就全都知道了**——分别来自模型目录与 pydantic-ai
    的 model profile。

    ``info`` 是 :class:`ModelInfo` 或 ``None``（目录里没有这个 id）。
    """
    # 绑成一个变量而不是 bool：`declared` 不是 None 时 info 一定不是 None 这件事，
    # 类型检查器看不出来——于是下面每一处 info.xxx 都被报成"None 没有这个属性"。
    declared = info if info is not None and info.declares_capabilities else None
    profile: Any = {}
    try:
        profile = make_model(model_id, provider).profile
    except Exception:  # 模型名不被 provider 接受——那是另一条错误路径，这里不掺和
        profile = {}

    def tri(ok: bool | None) -> str:
        return "unknown" if ok is None else ("yes" if ok else "no")

    checks = [
        CapabilityCheck(
            "reasoning",
            "深度思考",
            tri(declared.supports_reasoning if declared is not None else None),
            "模型目录声明支持推理才算。没声明的模型勾了「深度思考」也不会真的想。",
        ),
        CapabilityCheck(
            "web_search",
            "联网搜索",
            # 问 profile，不问目录。目录里的 `web_search_options` 是 OpenAI 自家那个
            # 字段的支持情况，而 OpenRouter 根本不实现它（实测塞非法值照样 200）。
            # 星槎走的是 OpenRouter 的 `plugins`（见 websearch_to_plugin），那是一层
            # 通用注入，与模型是否声明无关。而这个 flag 对 OpenRouter 全系为 True、
            # 厂商直连全 False，恰好等于"经 plugins 能不能搜"，所以判据成立。
            tri(bool(profile.get("openai_chat_supports_web_search", False))),
            "由上游去搜。OpenRouter 这类上游整体放行，厂商直连一律不放行。",
        ),
        CapabilityCheck(
            "native_schema",
            "原生结构化输出（T1）",
            tri(
                native_ok(model_id, provider, catalog_says=declared.supports_native_schema)
                if declared is not None
                else None
            ),
            "模型目录与调用通道都支持才算。有一边不支持就会自动降级到 T2。",
        ),
        CapabilityCheck(
            "tools",
            "工具调用（T2 的工具通道）",
            tri(declared.supports_tools if declared is not None else None),
            "不支持时 T2 请把「schema 送达方式」改成提示词通道，否则每次都 400。",
        ),
        CapabilityCheck(
            "multimodal",
            "图片 / 文件输入",
            tri(bool(declared.input_modalities - {"text"}) if declared is not None else None),
            "即使模型支持，星槎现在也只发文本；收到非文本内容会明确报错，不会静默丢掉。"
            "这一栏供你选模型时参考。",
        ),
    ]
    return checks


def make_model(model_id: str, provider: Provider) -> OpenAIChatModel:
    """构造 model。

    用 ``OpenAIChatModel`` 而不是 ``OpenRouterModel``：后者对响应缺 ``provider`` 字段
    会硬失败，而中转（New API 一类）不保证回传那个字段，走中转正是这个项目的用途。
    代价是拿不到上游的 prompt-cache 计价优化，但费用主价源已经是模型目录的单价。

    别好心改回去——中转到底回不回传 ``provider``，只能自己抓一次上游响应看。
    """
    return OpenAIChatModel(_strip_prefix(model_id), provider=provider)


# =============================================================================
# 提示词组装（用户模板 + 少样本）
# =============================================================================

#: 用户提示词模板里代表"调用方发来的那段话"的占位符。
PROMPT_PLACEHOLDER: Final = "{{input}}"

#: 星槎自己的东西放进 ``AgentSpec.metadata`` 的这个命名空间下：官方 schema 是
#: ``additionalProperties: false``，加顶层字段会被打回，而 ``metadata`` 是官方留的
#: 自由字典；带命名空间是为了不和别人写进去的东西撞。
#:
#: 代价：上游不解释这里的任何东西，模板与示例是星槎在运行时应用的，所以导出物不能
#: 只把 metadata 带走了事——见 exporter，它把两者烤进 run.py。
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

    读取路径宽容：库里可能存着更早版本写的 spec，老 Agent 不该因为少个键就跑不起来。
    严格校验在写入路径（:func:`validate_prompting`）。
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

    模板非空却不含占位符必须拦下：那样调用方发来的内容会被整个丢掉，每次都拿同一段
    固定文本去问模型，表现是"Agent 好像不看我的输入"而表单上一切正常。
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
    """把 :class:`Prompting` 写回 spec 的 metadata。空则不写键——否则每个 spec 里都多
    一坨没内容的结构，导出的 agent.yaml 也跟着脏。
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


#: 导出物里记录来源的两个键：分组与档位。它们在库里是列而不是 spec 字段，所以只在
#: 导出那一刻拓进 metadata、导入时读完即删——长期留副本的话，后台改一次分组就不同步，
#: 而不同步的那份会在下次导出时胜出。
ORIGIN_GROUP: Final = "group"
ORIGIN_TIER: Final = "tier"


def stamp_origin(spec: dict[str, Any], *, group: str | None, tier: str | None) -> dict[str, Any]:
    """导出：把分组与档位记进 ``metadata.xingcha``，让导出物能被完整还原。

    不记的话导到另一台星槎会静默丢两样：分组掉回默认组，档位退回自动判档——T1+/T3
    一律变成 T2，而那是保证方式与花费都不同的另一档，页面上看不出来。

    合并而不是覆盖：用户模板、少样本、输出通道住在同一个命名空间里。
    """
    body = {k: v for k, v in ((ORIGIN_GROUP, group), (ORIGIN_TIER, tier)) if v}
    if not body:
        return spec
    metadata = dict(spec.get("metadata") or {})
    metadata[SPEC_NS] = {**(metadata.get(SPEC_NS) or {}), **body}
    return {**spec, "metadata": metadata}


def take_origin(spec: dict[str, Any]) -> tuple[str | None, str | None]:
    """导入：取出分组与档位，并从 spec 里删掉（库里有列，不留副本）。

    原地改 spec。读不出来给 ``(None, None)``，调用方退回原有行为。
    """
    ns = (spec.get("metadata") or {}).get(SPEC_NS)
    if not isinstance(ns, dict):
        return None, None
    group = ns.pop(ORIGIN_GROUP, None)
    tier = ns.pop(ORIGIN_TIER, None)
    if not ns:  # 摘干净之后空了就别留一坨空结构
        spec["metadata"].pop(SPEC_NS, None)
        if not spec["metadata"]:
            spec.pop("metadata", None)
    return (
        group.strip() if isinstance(group, str) and group.strip() else None,
        tier.strip() if isinstance(tier, str) and tier.strip() else None,
    )


# =============================================================================
# 构造（数据库行 → 可执行的 Agent）
# =============================================================================


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

    #: 两阶段（T1+）的第一阶段：不带任何格式约束，纯自由推理。只有 T1+ 有，存在的
    #: 意义是让推理那一步不受格式约束干扰。
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
    # 联合类型，不是 OpenRouterProvider —— 用哪一种由 base_url 决定（见 make_provider），
    # 而这里只是把它转交给 make_model。标窄了的话厂商直连那条路每次都是类型错误。
    provider: Provider,
    options: BuildOptions,
    concurrency: Any = None,
) -> AgentRuntime:
    """``agent_version`` 的一行 → 可执行的 Agent。

    ``spec_json`` 原样来自数据库，这里是唯一解释它的地方。
    """
    spec = json.loads(spec_json) if isinstance(spec_json, str) else dict(spec_json)
    # 库里可能存着 0.1 时期写下的、from_spec 收不下的 capability 形状。
    spec = runnable_capabilities(spec)
    # 「联网搜索」必须在这里翻成 OpenRouter 的 plugins，否则勾了等于没勾。
    spec = websearch_to_plugin(spec)
    schema = json.loads(out_schema) if isinstance(out_schema, str) else out_schema

    model_id = spec.get("model")
    if not isinstance(model_id, str) or not model_id:
        # AgentSpec 层面 model 其实是**可选**的（实测），所以 model_validate 不会拦，
        # 错误会推迟到 from_spec 抛 UserError。在这里显式拦下，报错更靠近原因。
        raise AgentSpecInvalid("Agent 定义里没有 model")

    # make_model 要在 try 里：它会抛 UserError（模型名不被 provider 接受之类），放在
    # 外面的话异常一路冒到最外层，变成一句"服务内部错误"——而这是每次都发生、最需要
    # 说清原因的一类失败。
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
        # 必须显式传 output_type（两种都实测过）：只把 schema 留在 spec 里，from_spec
        # 会设成不校验的 StructuredDict；既 pop 掉又不传则退化成 str，校验器收到原始
        # JSON 字符串，连合法输出都会被打到重试耗尽。
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

    # 两阶段（T1+）：用同一份 spec 再造一个不带输出约束的 agent 做第一步，让推理不受
    # 格式约束干扰。代价是约两倍的调用成本。
    reason_agent: Agent | None = None
    if tier is Tier.T1P and schema is not None:
        # 必须从 spec 里去掉 output_schema。传 output_type=str 不够（实测）：str 正是
        # 那个参数的默认值，pydantic-ai 分不清"显式传了 str"与"根本没传"，照样回落到
        # spec 里的 output_schema，第一阶段仍然带着约束。
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


# =============================================================================
# 表单 ↔ spec
# =============================================================================
#
# 服务的是后台的 Agent 编辑页，但留在 core 而不是 web：「表单里有哪些模型参数、哪些
# 能力可勾」是 pydantic-ai 的知识，搬到 web 去升级时就要改两个地方。


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

    ``instrument`` 不是 AgentSpec 字段（实测），对应的是 ``Instrumentation`` capability
    ——表单的"可观测"开关要写进 capabilities，不能建顶层输入项。
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
        # 必须是裸 int 或 {'output': n}。2.35.3 新增的 {'tools': n} 不影响 output 校验
        # 重试，写成那样会让重试预算看起来设了、实际没设。
        spec["retries"] = retries
    if prompting is not None:
        prompting_to_spec(spec, prompting)
    return spec


def model_settings_from_form(raw: dict[str, str]) -> dict[str, Any]:
    """表单里的模型参数 → ``model_settings`` dict。

    空字符串一律丢弃，不写成 0 或 null：留空是"不设这一项、用上游默认"，而
    ``temperature: 0`` 是一条明确指令，混同会把每个 Agent 变成确定性输出。

    类型按官方 schema 走（整数字段收 int，其余 float）——收错类型不会报错，
    ``extra='ignore'`` 会静默丢掉那一项。
    """
    ints = {"max_tokens", "seed", "top_k"}
    out: dict[str, Any] = {}
    for field, _, _, _ in model_settings_fields():
        text = (raw.get(field) or "").strip()
        if not text:
            continue
        try:
            out[field] = int(text) if field in ints else float(text)
        except ValueError as e:
            from ..foundation.errors import AgentSpecInvalid

            raise AgentSpecInvalid(f"{field} 不是合法的数字：{text!r}") from e

    # 枚举项只收闭集里的值：不校验的话手改过的表单能把任意字符串塞进 spec，而上游
    # 对无效值回的 400 离"你在下拉框里选了什么"很远。
    for field, _, _, _, options in choice_settings_fields():
        text = (raw.get(field) or "").strip()
        if not text:
            continue
        if text not in options:
            from ..foundation.errors import AgentSpecInvalid

            raise AgentSpecInvalid(f"{field} 只能是 {'/ '.join(options)} 之一，收到 {text!r}")
        out[field] = text
    return out


def capability_names(caps: list[Any]) -> set[str]:
    """从 spec 的 capabilities 里取出能力名。三种形状都要认。

    库里可能存着 ``model_dump()`` 规范化出的 ``[{"name": "Thinking"}]``（新写入的由
    :func:`runnable_capabilities` 降回裸字符串），手写的 agent.yaml 里还可能是
    ``[{"Thinking": {...参数}}]``。只认一种的下场：反填时把第一个 key 当成能力名，
    每个 Agent 都被读成开了一个叫 ``name`` 的能力——编辑页所有勾是空的，一保存就把
    用户设过的能力全清掉。
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

    反填不了的话编辑就等于重填，"只想改一句提示词"会把之前设过的 temperature 清掉。
    """
    settings = spec.get("model_settings") or {}
    names = capability_names(spec.get("capabilities") or [])
    prompting = prompting_from_spec(spec)
    return {
        "settings": {
            k: settings.get(k, "")
            for k, *_ in (*model_settings_fields(), *choice_settings_fields())
        },
        "capabilities": names,
        "instrumented": CAPABILITY_INSTRUMENTATION in names,
        "user_template": prompting.user_template,
        "examples": list(prompting.examples),
        "output_channel": prompting.output_channel,
    }
