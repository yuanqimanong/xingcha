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
from pydantic_ai.providers.openrouter import OpenRouterProvider

from .. import contract as C
from ..contract import Tier
from ..errors import AgentBuildFailed, AgentSpecInvalid
from .costsink import CostSink, make_hook
from .guarantee import GuaranteeCounters, attach_validator, limits_for, output_spec
from .upstream import UpstreamConfig, attribution_headers

log = logging.getLogger(__name__)


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
    return parsed.model_dump(by_alias=True, exclude_none=True)


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


def make_provider(
    cfg: UpstreamConfig, *, timeout: float, cost_sink: CostSink | None = None
) -> OpenRouterProvider:
    """构造 provider。

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
    return OpenRouterProvider(openai_client=client)


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
FORM_CAPABILITIES: Final[tuple[tuple[str, str, str], ...]] = (
    ("Thinking", "思考", "让模型先想再答。只有支持推理的模型有效，会多花 token。"),
    ("WebSearch", "联网搜索", "模型可以自己搜。**上游必须支持**，否则请求会被拒。"),
    ("WebFetch", "网页抓取", "模型可以自己取网页正文。同样依赖上游支持。"),
    ("ToolSearch", "工具搜索", "工具很多时让模型先检索再调用。"),
    ("ImageGeneration", "图像生成", "让模型能出图。上游不支持时请求会被拒。"),
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


def form_capabilities() -> tuple[tuple[str, str, str], ...]:
    """表单要用的能力，**对着官方 CAPABILITY_TYPES 校验过**。"""
    known = set(declarable_capabilities())
    missing = [name for name, _, _ in FORM_CAPABILITIES if name not in known]
    if missing:  # pragma: no cover
        raise RuntimeError(f"这些能力在 pydantic-ai 里不存在了：{missing}")
    if CAPABILITY_INSTRUMENTATION not in known:  # pragma: no cover
        raise RuntimeError("Instrumentation 能力不见了，可观测的按 Agent 开关无从实现")
    return FORM_CAPABILITIES


def _strip_prefix(model: str) -> str:
    """表单可能存成 ``openrouter:openai/gpt-5``；provider 已显式给出，去掉前缀。"""
    return model.split(":", 1)[1] if model.startswith("openrouter:") else model


def make_model(model_id: str, provider: OpenRouterProvider) -> OpenAIChatModel:
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
    schema = json.loads(out_schema) if isinstance(out_schema, str) else out_schema

    model_id = spec.get("model")
    if not isinstance(model_id, str) or not model_id:
        # AgentSpec 层面 model 其实是**可选**的（实测），所以 model_validate 不会拦，
        # 错误会推迟到 from_spec 抛 UserError。在这里显式拦下，报错更靠近原因。
        raise AgentSpecInvalid("Agent 定义里没有 model")

    kwargs: dict[str, Any] = {
        "model": make_model(model_id, provider),
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
        kwargs["output_type"] = output_spec(tier, schema, max_retries=options.max_retries)

    try:
        agent = Agent.from_spec(spec, **kwargs)
    except (ValidationError, ValueError, UserError) as e:
        # 三类都可能出现：ValidationError 来自字段类型错，ValueError 来自未知
        # capability 名，UserError 来自 model 缺失或未知模型名。
        raise AgentBuildFailed(f"{type(e).__name__}: {e}") from e

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
            raise AgentBuildFailed(f"两阶段的推理 agent 构造失败：{type(e).__name__}: {e}") from e

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
    return {
        "settings": {k: settings.get(k, "") for k, _, _ in model_settings_fields()},
        "capabilities": names,
        "instrumented": CAPABILITY_INSTRUMENTATION in names,
    }


#: 供 doctor 与设置页显示。
UPSTREAM_MODEL_PREFIX = "openrouter:"
CONTRACT_TIER_VALUES = tuple(t.value for t in C.Tier)
