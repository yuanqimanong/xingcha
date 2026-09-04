"""输出保证。

**这是整个项目的技术核心。**

要解决的问题：pydantic-ai 的声明式路径**不做运行时校验**。把 schema 放进
``AgentSpec.output_schema``，它只被用来生成发给模型的指令；Pydantic 侧只校验
"是不是一个 dict"。实测同一份三重违规数据（缺必填 + 类型错 + 超 maxItems）
在命令式路径（真 Pydantic model）下被拦截并重试，在声明式路径下**原样放行**。

这不是框架缺陷，是声明式路径丢失了命令式路径已有的保证。星槎要做的就是把它补回来。

------------------------------------------------------------------------------
四档，v0.2 只实现 T2
------------------------------------------------------------------------------

============ ================================ ========== ============ ========
档            机制                              形状保证    内容风险      成本
============ ================================ ========== ============ ========
T1           ``NativeOutput(strict=True)``     最强       **有对齐税**  单次
T2           ``ToolOutput(strict=False)`` +    强         无对齐税      重试放大
             jsonschema + ``ModelRetry``
T1+          两阶段：自由推理 → 再格式化          最强       最低         约两倍
T3           ``PromptedOutput``（仅提示）        无         无           单次
============ ================================ ========== ============ ========

四档的**映射**都在这里（各三行，不值得拆开），但 v0.2 的表单只开放 T2。
T1 还缺两件东西才能安全地交给用户：按模型能力自动判档，以及把 ``strict=True``
会静默把可选字段提升为必填这件事在表单上说清楚（见 :func:`t1_rewrites_schema`）。
没有那两件就开放 T1，等于让用户在不知情的情况下承担对齐税。

------------------------------------------------------------------------------
两个计数器为什么必须分开（实测）
------------------------------------------------------------------------------

``retries=2`` 且持续违规时：模型被调用 3 次，校验器被调用 3 次，但**真实重试只有
2 次**——最后一次校验失败后预算已耗尽，不再重试。所以：

* ``schema_violations`` = 自己数的违规次数（3）
* ``schema_retries``    = ``RunContext.retry``（2，框架给的真实序号）

只留自数的那个，会让失败 run 的重试数系统性偏移一格——而那恰恰是最需要精确告警的
一类 run。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from pydantic_ai import (
    Agent,
    ModelRetry,
    NativeOutput,
    PromptedOutput,
    RunContext,
    StructuredDict,
    ToolOutput,
    UsageLimits,
)

from ..contract import Tier
from .schema_guard import make_validator

log = logging.getLogger(__name__)


@dataclass
class GuaranteeCounters:
    """一次运行的校验计数。由闭包持有，运行结束后读。"""

    #: 自数的违规次数。耗尽时 = 1 + retries。
    violations: int = 0
    #: 框架给的真实重试序号（``RunContext.retry`` 的最大值）。
    retries: int = 0
    #: 原生约束档下仍然违规的次数——上游没兑现 strict，值得单独告警。
    provider_noncompliance: int = 0
    #: 最后一次的错误详情，供 422 响应体使用。
    last_error: str = ""


def output_spec(tier: Tier, schema: dict[str, Any], *, max_retries: int) -> Any:
    """把档位翻译成 pydantic-ai 的 ``output_type``。

    这个返回值必须传进 ``Agent.from_spec(..., output_type=...)``。

    **不能只把 schema 留在 spec 里然后指望它生效**：那样 ``from_spec`` 会把
    ``output_type`` 设成一个不校验的 ``StructuredDict``；而如果既 pop 掉
    ``output_schema`` 又不传 ``output_type``，它会退化成 ``str``——于是校验器收到的是
    原始 JSON **字符串**，对 object schema 必然报 "is not of type 'object'"，
    **连完全合法的模型输出都会被打到重试耗尽**。这是实测过的。
    """
    sd = StructuredDict(schema)
    match tier:
        case Tier.T2:
            # strict=False 是必需的，不是默认值的同义写法。
            #
            # 不写的话 pydantic-ai 会按 model profile 把 strict 推断成 true 并发上去
            # （实测），于是 T2 在 OpenAI 系模型上同样承担对齐税——而 T2 存在的理由
            # 恰恰是"不承担对齐税"。前端给用户显示的保证等级也就成了假的。
            return ToolOutput(sd, strict=False, max_retries=max_retries)
        case Tier.T1 | Tier.T1P:
            return NativeOutput(sd, strict=True)
        case Tier.T3:
            # PromptedOutput 不接受 strict —— 传了直接 TypeError
            return PromptedOutput(sd)


def attach_validator(agent: Agent, tier: Tier, schema: dict[str, Any]) -> GuaranteeCounters:
    """挂运行时校验，返回计数器。

    档位之间的差异**由这个函数制造，不是框架行为**：实测四种输出模式下
    ``output_validator`` 被调用的次数逐位相同。所以 "T3 不校验" 是星槎的应用层策略，
    测试要断言"本项目没有注册校验器"，而不是断言框架不校验——否则把 T3 误传成 T2
    也测不出来。
    """
    counters = GuaranteeCounters()

    if tier is Tier.T3:
        # 用户显式选择"只把 schema 当提示"。不挂校验器。
        return counters

    validator = make_validator(schema)

    @agent.output_validator
    def _validate(ctx: RunContext, data: Any) -> Any:
        errors = sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))
        counters.retries = max(counters.retries, ctx.retry)
        if not errors:
            return data

        counters.violations += 1
        if tier in (Tier.T1, Tier.T1P):
            # 原生约束下还能违规，说明上游没兑现 strict。照样重试——绝不把脏数据
            # 交给调用方——但单独计数，因为它是上游的问题不是用户的问题。
            counters.provider_noncompliance += 1

        detail = "; ".join(
            f"{'/'.join(map(str, e.absolute_path)) or '<根>'}: {e.message}" for e in errors[:5]
        )
        counters.last_error = detail
        raise ModelRetry(f"输出不符合 schema：{detail}")

    return counters


def limits_for(
    *, max_retries: int, max_tool_steps: int, max_tokens: int, max_cost_usd: Decimal | None
) -> UsageLimits:
    """一次运行的护栏。

    三处与文档写法不同，每一处都是实测出来的：

    ``request_limit`` 不能只是 ``max_retries + 1``
        它计的是**所有**模型请求，工具调用的往返也算一次。实测一个只有 1 次工具往返、
        0 次 schema 重试的 run 就消耗 ``requests=2``。按 ``max_retries + 1`` 算的话，
        任何带工具的 Agent 都会在第一次工具调用后被打断，而且用户看到的是"限流"
        而不是真实原因。

    ``total_tokens_limit`` 是必填而不是可选
        ``cost_limit`` 对 genai-prices 认不出的模型**静默失效**（只发一条
        ``CostNotFoundWarning``），而实测在售模型里约三分之一查不到价。也就是说
        费用护栏在三分之一的模型上是摆设。token 上限永远可执行，必须作为硬兜底。

    ``cost_limit`` 必须是 ``Decimal``
        标注是 Decimal 但不校验，传 float 今天能跑；金额全链路用 Decimal，
        不给未来留一个精度坑。
    """
    return UsageLimits(
        request_limit=(max_retries + 1) + max_tool_steps,
        total_tokens_limit=max_tokens,
        cost_limit=max_cost_usd,
    )


def t1_rewrites_schema(schema: dict[str, Any]) -> list[str]:
    """T1 档下会被上游**静默提升为必填**的可选字段。

    ``NativeOutput(strict=True)`` 会把所有可选字段塞进 ``required``（实测：一个
    ``required: ["title"]`` 而 ``score`` 可选的 schema，发到线上变成
    ``required: ["title","score"]``，且没有加成可空类型）。

    也就是说用户在表单里标为可选的字段，在 T1 档下会变成模型**必须**输出的字段。
    这对用户构成隐性违约，所以表单上必须提前说清楚——这个函数就是给表单用的。
    """
    props = schema.get("properties")
    if not isinstance(props, dict):
        return []
    required = set(schema.get("required") or [])
    return sorted(name for name in props if name not in required)


@dataclass(frozen=True, slots=True)
class TierChoice:
    """判档结果。"""

    tier: Tier
    #: 降级说明。为空表示用户要什么给了什么。
    downgraded_from: Tier | None = None
    reason: str = ""


def resolve_tier(requested: Tier | None, *, has_schema: bool, native_ok: bool) -> TierChoice:
    """决定实际使用的档位。

    ``native_ok`` 来自模型目录的 ``structured_outputs``——**不能看
    ``response_format``**：实测两者不等价（今天 424 个在售模型里后者 365、前者 340，
    有 25 个只有后者），混用会把 T2 误判成 T1，于是对用户谎称"有原生保证"。

    未知模型一律当作不支持：宁可降级到 T2 多花点重试成本，也不能谎称有保证。
    """
    if not has_schema:
        return TierChoice(Tier.T3, reason="没有配置输出 schema，按纯文本处理")

    if requested in (Tier.T1, Tier.T1P) and not native_ok:
        return TierChoice(
            Tier.T2,
            downgraded_from=requested,
            reason="该模型未声明支持原生结构化输出，已降级为 T2（校验后重试）",
        )

    return TierChoice(requested or Tier.T2)


#: 每一档的对外说明。表单与 API 都从这里取，避免两处措辞漂移。
TIER_INFO: dict[Tier, dict[str, str]] = {
    Tier.T1: {
        "name": "原生约束",
        "shape": "最强",
        "content": "有对齐税：格式约束会削弱推理，且**可选字段会被上游提升为必填**",
        "cost": "单次",
        "needs_native": "yes",
    },
    Tier.T2: {
        "name": "校验后重试",
        "shape": "强",
        "content": "无对齐税，schema 原样发给上游",
        "cost": "最坏 1+重试次数 倍（默认重试 2 次即最多 3 倍）",
        "needs_native": "no",
    },
    Tier.T1P: {
        "name": "两阶段",
        "shape": "最强",
        "content": "最低：先自由推理再格式化，格式约束不参与推理那一步",
        "cost": "约两倍（两次模型调用）",
        "needs_native": "yes",
    },
    Tier.T3: {
        "name": "仅提示",
        "shape": "无",
        "content": "无——schema 只进提示词，输出不合规也照样返回",
        "cost": "单次",
        "needs_native": "no",
    },
}


#: 表单开放的档位。四档全开。
#:
#: T1 与 T1P 是在自动判档（:func:`resolve_tier` 查目录的 ``structured_outputs``）与
#: "可选字段会被提升为必填"的表单提示（:func:`t1_rewrites_schema` + schema_lint）
#: 都到位之后才开放的。缺任何一件就开放 T1，等于让用户在不知情的情况下承担对齐税。
AVAILABLE_TIERS: tuple[Tier, ...] = (Tier.T1, Tier.T2, Tier.T1P, Tier.T3)


def guard_counters(counters: GuaranteeCounters, *, tier: Tier) -> None:
    """把值得告警的情况记进日志。"""
    if counters.provider_noncompliance:
        log.warning(
            "档位 %s 下上游 %d 次未兑现 strict —— 原生约束没有生效，实际保证等同 T2",
            tier.value,
            counters.provider_noncompliance,
        )
    if counters.violations > 2:
        log.info(
            "schema 违规 %d 次（真实重试 %d 次）。持续违规会成倍放大 token 消耗，"
            "考虑简化 schema 或换更强的模型。",
            counters.violations,
            counters.retries,
        )


# =============================================================================
# T1+ 两阶段
# =============================================================================

#: 第二阶段的指令。
#:
#: **只做格式化，不允许改事实。** 两阶段的价值是让推理那一步不受格式约束干扰；
#: 如果第二步顺手"改进"内容，那就等于又引入了一次未受控的生成，反而比 T1 更糟。
FORMAT_INSTRUCTIONS = (
    "把下面这段内容整理成规定的结构。\n"
    "只做格式转换：不要新增事实、不要删改事实、不要补充推测。"
    "原文里没有的信息，对应字段留空或按 schema 的规则处理。"
)


def format_prompt(draft: str) -> str:
    return f"{FORMAT_INSTRUCTIONS}\n\n---\n\n{draft}"


def two_stage_request_budget(max_retries: int, max_tool_steps: int) -> int:
    """两阶段的 ``request_limit``。

    两个阶段各自会发请求，而 ``UsageLimits`` 是**按次运行**给的——所以每一阶段的
    预算要分别算，不能把两阶段的总和塞给其中一个。这里返回的是**单阶段**的值，
    由 RunService 给两个 agent 各设一份。
    """
    return (max_retries + 1) + max_tool_steps
