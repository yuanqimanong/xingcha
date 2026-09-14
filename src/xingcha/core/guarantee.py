"""输出保证。整个项目的技术核心。

要解决的问题：pydantic-ai 的声明式路径不做运行时校验。放进 ``AgentSpec.output_schema``
的 schema 只被用来生成发给模型的指令，Pydantic 侧只校验"是不是一个 dict"。实测同一份
三重违规数据（缺必填 + 类型错 + 超 maxItems）在命令式路径下被拦截并重试，在声明式
路径下原样放行。星槎要做的就是把这层保证补回来。

四档，全部实现：

============ ================================ ========== ============ ========
档            机制                              形状保证    内容风险      成本
============ ================================ ========== ============ ========
T1           ``NativeOutput(strict=True)``     最强       **有对齐税**  单次
T2           ``ToolOutput(strict=False)`` +    强         无对齐税      重试放大
             jsonschema + ``ModelRetry``
T1+          两阶段：自由推理 → 再格式化          最强       最低         约两倍
T3           ``PromptedOutput``（仅提示）        无         无           单次
============ ================================ ========== ============ ========

四档的映射都在这里。表单开放哪几档见 :data:`AVAILABLE_TIERS`；T1 的对齐税靠
:data:`TIER_INFO` 的说明与 :func:`t1_rewrites_schema` 在选之前讲清楚，而不是靠不给选。

两个计数器必须分开（实测）：``retries=2`` 且持续违规时模型被调用 3 次、校验器被调用
3 次，但真实重试只有 2 次（最后一次失败后预算已耗尽）。所以 ``schema_violations`` 是
自己数的违规次数，``schema_retries`` 取 ``RunContext.retry``——只留前者会让失败 run 的
重试数系统性偏移一格，而那恰恰是最需要精确告警的一类 run。
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


#: T2 把 schema 递给模型的两条通道。档位的保证不受它影响——T2 的定义是"校验后重试"，
#: 不是"走 tools"，校验器与重试预算两边完全一样。
#:
#: 分成一个选项是因为有的上游没有 tools 这条路：DeepSeek 的思考模式不接受
#: ``tool_choice``，于是 T2 在它上面每次都 400，而唯一还能出结构化输出的 T3 恰好是那个
#: 不做校验的档——保证阶梯整个塌成"没有保证"。换条通道就能把 T2 救回来，代价只是
#: schema 以提示词形式送达（遵守度略低，而那正是校验+重试要兜的东西）。
OUTPUT_CHANNELS: dict[str, str] = {
    "tool": "工具通道（默认）",
    "prompt": "提示词通道",
}


def output_spec(
    tier: Tier, schema: dict[str, Any], *, max_retries: int, channel: str = "tool"
) -> Any:
    """把档位翻译成 pydantic-ai 的 ``output_type``，必须传进
    ``Agent.from_spec(..., output_type=...)``。

    不能只把 schema 留在 spec 里指望它生效（两种都实测过）：那样 ``from_spec`` 会设成
    不校验的 ``StructuredDict``；既 pop 掉 ``output_schema`` 又不传 ``output_type`` 则
    退化成 ``str``，校验器收到原始 JSON 字符串，连合法输出都会被打到重试耗尽。

    ``channel`` 只对 T2 有意义，见 :data:`OUTPUT_CHANNELS`。
    """
    sd = StructuredDict(schema)
    match tier:
        case Tier.T2:
            if channel == "prompt":
                # 仍然是 T2：下面 attach_validator 照挂校验器、照用重试预算。
                # 变的只是 schema 以提示词而非工具签名送达。
                return PromptedOutput(sd)
            # strict=False 是必需的，不是默认值的同义写法：不写的话 pydantic-ai 会按
            # model profile 把它推断成 true 并发上去（实测），T2 在 OpenAI 系模型上就
            # 同样承担了对齐税——而 T2 存在的理由恰恰是不承担它。
            return ToolOutput(sd, strict=False, max_retries=max_retries)
        case Tier.T1 | Tier.T1P:
            return NativeOutput(sd, strict=True)
        case Tier.T3:
            # PromptedOutput 不接受 strict —— 传了直接 TypeError
            return PromptedOutput(sd)


def attach_validator(agent: Agent, tier: Tier, schema: dict[str, Any]) -> GuaranteeCounters:
    """挂运行时校验，返回计数器。

    档位之间的差异由这个函数制造，不是框架行为：实测四种输出模式下 ``output_validator``
    被调用的次数逐位相同。所以"T3 不校验"是星槎的应用层策略，测试要断言"本项目没有注册
    校验器"，而不是断言框架不校验——否则把 T3 误传成 T2 也测不出来。
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
    """一次运行的护栏。三处与文档写法不同，每一处都是实测出来的：

    ``request_limit`` 不能只是 ``max_retries + 1``
        它计的是所有模型请求，工具往返也算一次（实测一个 0 次 schema 重试的 run 就消耗
        ``requests=2``）。按 ``max_retries + 1`` 算的话，任何带工具的 Agent 都会在第一次
        工具调用后被打断，而用户看到的是"限流"。

    ``total_tokens_limit`` 是必填而不是可选
        ``cost_limit`` 对 genai-prices 认不出的模型静默失效（只发一条
        ``CostNotFoundWarning``），而实测在售模型里约三分之一查不到价。token 上限永远
        可执行，必须作为硬兜底。

    ``cost_limit`` 必须是 ``Decimal``
        标注是 Decimal 但不校验，传 float 今天能跑。金额全链路用 Decimal。
    """
    return UsageLimits(
        request_limit=(max_retries + 1) + max_tool_steps,
        total_tokens_limit=max_tokens,
        cost_limit=max_cost_usd,
    )


def t1_rewrites_schema(schema: dict[str, Any]) -> list[str]:
    """T1 档下会被上游静默提升为必填的可选字段。

    ``NativeOutput(strict=True)`` 会把所有可选字段塞进 ``required``（实测：``score``
    可选的 schema 发到线上变成 ``required: ["title","score"]``，且没有加成可空类型）。
    用户标为可选的字段因此变成模型必须输出的字段，表单上要提前说清楚——这个函数就是
    给表单用的。
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

    ``native_ok`` 来自模型目录的 ``structured_outputs``，不能看 ``response_format``：
    实测两者不等价（424 个在售模型里有 25 个只有后者），混用会把 T2 误判成 T1。

    未知模型一律当作不支持：宁可降级到 T2 多花点重试成本，也不能谎称有保证。
    """
    if not has_schema:
        # **不能报 T3。** T3 的含义是"schema 只进提示词、不做校验"——那至少还有一份
        # schema；纯文本 Agent 一份都没有。调用方读 x_xingcha.tier 是为了知道这次
        # 调用有没有结构保证、代价是什么，而 T3 对纯文本是个错误答案。
        return TierChoice(Tier.NONE, reason="没有配置输出 schema，按纯文本处理")

    if requested in (Tier.T1, Tier.T1P) and not native_ok:
        return TierChoice(
            Tier.T2,
            downgraded_from=requested,
            reason="该模型未声明支持原生结构化输出，已降级为 T2（校验后重试）",
        )

    return TierChoice(requested or Tier.T2)


#: 每一档的对外说明。表单与 API 都从这里取，避免两处措辞漂移。``pick`` 放在最前：读者
#: 打开这个下拉时唯一想知道的是"什么时候该选它"，而不是在 shape/content/cost 三个维度
#: 之间自己做换算。
TIER_INFO: dict[Tier, dict[str, str]] = {
    Tier.T1: {
        "name": "原生约束",
        "pick": "要最省钱、字段都是必填的简单抽取",
        "how": "上游在解码时就不让模型写出不合 schema 的 token，所以一次就对，不重试。",
        # 用大白话先说清后果，再把项目自己的术语（对齐税）括进去——只写术语，
        # 读者得先学会那个词才知道自己在选什么；只写大白话，别处的文档又对不上。
        "catch": "可选字段会被上游提升为必填，模型于是被迫编一个值填进去；"
        "格式约束还会削弱推理，复杂任务的答案质量会掉（这两条合称「对齐税」）。",
        "shape": "最强",
        "content": "有对齐税：格式约束会削弱推理，且可选字段会被上游提升为必填",
        "cost": "单次",
        "needs_native": "yes",
    },
    Tier.T2: {
        "name": "校验后重试",
        "pick": "拿不准就选它",
        "how": "模型自由作答，星槎拿 schema 校验；不合规就把错误详情打回去让它重写。",
        "catch": "不合规时会多调几次模型，最坏 1+重试次数 倍的钱。",
        "shape": "强",
        "content": "无对齐税，schema 原样发给上游",
        "cost": "最坏 1+重试次数 倍（默认重试 2 次即最多 3 倍）",
        "needs_native": "no",
    },
    Tier.T1P: {
        "name": "两阶段",
        "pick": "任务需要真的思考，同时又要严格的结构",
        "how": "先让模型不带任何格式约束自由推理，再单独调一次只做格式化。",
        "catch": "两次模型调用，约两倍的钱，而且慢一倍。",
        "shape": "最强",
        "content": "最低：先自由推理再格式化，格式约束不参与推理那一步",
        "cost": "约两倍（两次模型调用）",
        "needs_native": "yes",
    },
    # 纯文本。**不在 AVAILABLE_TIERS 里**（表单的"纯文本"是不选档位，不是选它），
    # 但按档位取说明的地方会拿到它，所以这条必须在。
    Tier.NONE: {
        "name": "纯文本",
        "pick": "不需要结构化输出",
        "how": "没有配置 schema，模型返回什么就原样给调用方。",
        "catch": "没有任何结构保证——本来也没要。",
        "shape": "不适用",
        "content": "纯文本，不做任何校验",
        "cost": "单次",
        "needs_native": "no",
    },
    Tier.T3: {
        "name": "仅提示",
        "pick": "只想给模型一个格式参考，不要任何强制",
        "how": "schema 只写进提示词，输出不做校验，模型返回什么就是什么。",
        "catch": "没有任何保证。字段缺了、类型不对，都得调用方自己兜。",
        "shape": "无",
        "content": "无——schema 只进提示词，输出不合规也照样返回",
        "cost": "单次",
        "needs_native": "no",
    },
}


#: 表单开放的档位。四档全开。T1 与 T1P 是在自动判档（:func:`resolve_tier`）与"可选字段
#: 会被提升为必填"的表单提示（:func:`t1_rewrites_schema` + schema_lint）都到位之后才开
#: 的——缺任何一件就开放 T1，等于让用户在不知情的情况下承担对齐税。
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

#: 第二阶段的指令。只做格式化，不允许改事实：两阶段的价值是让推理那一步不受格式约束
#: 干扰，第二步要是顺手"改进"内容，就等于又引入一次未受控的生成，反而比 T1 更糟。
FORMAT_INSTRUCTIONS = (
    "把下面这段内容整理成规定的结构。\n"
    "只做格式转换：不要新增事实、不要删改事实、不要补充推测。"
    "原文里没有的信息，对应字段留空或按 schema 的规则处理。"
)


def format_prompt(draft: str) -> str:
    return f"{FORMAT_INSTRUCTIONS}\n\n---\n\n{draft}"
