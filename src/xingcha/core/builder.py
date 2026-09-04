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
from typing import Any

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


def make_provider(cfg: UpstreamConfig, *, timeout: float) -> OpenRouterProvider:
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
    http = httpx2.AsyncClient(
        trust_env=False,
        timeout=httpx2.Timeout(timeout, connect=min(15.0, timeout)),
    )
    client = AsyncOpenAI(
        base_url=cfg.normalized_base(),
        api_key=cfg.api_key,
        max_retries=0,
        http_client=http,
        default_headers=attribution_headers(cfg) or None,
    )
    return OpenRouterProvider(openai_client=client)


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


#: 供 doctor 与设置页显示。
UPSTREAM_MODEL_PREFIX = "openrouter:"
CONTRACT_TIER_VALUES = tuple(t.value for t in C.Tier)
