"""输出保证。

**这是整个项目唯一的技术护城河，所以覆盖要密。**

论点是：pydantic-ai 的声明式路径不做运行时校验，星槎把它补回来。第一组测试先证明
那个论点成立（否则整个模块没有存在理由），后面几组证明补救真的有效。

单元层用 ``FunctionModel``：它能精确控制模型返回什么、被调用几次，而真实模型两样
都做不到。全部离线，不需要任何 key。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from xingcha.contract import Tier
from xingcha.core.guarantee import (
    AVAILABLE_TIERS,
    TIER_INFO,
    attach_validator,
    limits_for,
    output_spec,
    resolve_tier,
    t1_rewrites_schema,
)
from xingcha.core.schema_guard import validate_schema

SCHEMA = validate_schema(
    {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "score": {"type": "integer"},
            "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 2},
        },
        "required": ["title", "score"],
    }
)

#: 三重违规：缺必填 title、score 类型错、tags 超 maxItems
BAD: dict[str, Any] = {"score": "not-an-int", "tags": ["a", "b", "c"]}
GOOD: dict[str, Any] = {"title": "ok", "score": 7}


def make_agent(tier: Tier, payloads: list[Any], *, retries: int = 2):
    """构造一个受控的 Agent。返回 ``(agent, calls, counters)``。"""
    calls = {"n": 0}

    def fn(messages, info: AgentInfo):
        i = calls["n"]
        calls["n"] += 1
        body = payloads[min(i, len(payloads) - 1)]
        if info.output_tools:
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, body)])
        # PromptedOutput 走文本通道，不是工具通道
        return ModelResponse(parts=[TextPart(json.dumps(body))])

    agent = Agent.from_spec(
        {"model": "test"},
        model=FunctionModel(fn),
        output_type=output_spec(tier, SCHEMA, max_retries=retries),
        retries=retries,
    )
    counters = attach_validator(agent, tier, SCHEMA)
    return agent, calls, counters


# =============================================================================
# 论点：声明式路径确实不校验
# =============================================================================


class TestTheProblemIsReal:
    def test_declarative_path_passes_violations_through(self):
        """把 schema 放进 AgentSpec.output_schema 而不接管 output_type，
        三重违规数据会被**原样放行**，且 retries 不触发。

        这条是整个 guarantee 模块的存在理由。它要是不成立，这个模块就该删掉。
        """
        calls = {"n": 0}

        def fn(messages, info: AgentInfo):
            calls["n"] += 1
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, BAD)])

        agent = Agent.from_spec(
            {"model": "test", "output_schema": SCHEMA, "retries": 3},
            model=FunctionModel(fn),
        )
        result = agent.run_sync("x")
        assert result.output == BAD, "违规数据本该被放行——论点不成立了？"
        assert calls["n"] == 1, "retries 本不该触发"

    def test_popping_schema_without_output_type_degrades_to_text(self):
        """文档 §5.2 的写法（pop 掉 output_schema 又不传 output_type）会让
        output_type 退化成 str。

        后果不是"没有校验"，而是**连完全合法的输出都会被判违规**——校验器收到的是
        原始 JSON 字符串，对 object schema 必然报 "is not of type 'object'"。
        这是 H1，本项目最早发现的硬伤。
        """
        from pydantic_ai.models.test import TestModel

        agent = Agent.from_spec({"model": "test"}, model=TestModel())
        assert agent.output_type is str


# =============================================================================
# T2：校验后重试
# =============================================================================


class TestT2:
    def test_blocks_and_retries_then_raises(self):
        """持续违规：调用 1+retries 次后抛出。

        次数是 **1 + retries**，不是文档表格里写死的 3。这个数字直接进成本模型：
        retries=3 时最坏是 4 倍 token 而非 3 倍。
        """
        agent, calls, counters = make_agent(Tier.T2, [BAD], retries=2)
        with pytest.raises(UnexpectedModelBehavior):
            agent.run_sync("x")
        assert calls["n"] == 3
        assert counters.last_error, "必须带上最后一次的 schema 错误详情"
        assert "title" in counters.last_error

    @pytest.mark.parametrize(("retries", "expected"), [(1, 2), (2, 3), (3, 4)])
    def test_call_count_is_one_plus_retries(self, retries: int, expected: int):
        agent, calls, _ = make_agent(Tier.T2, [BAD], retries=retries)
        with pytest.raises(UnexpectedModelBehavior):
            agent.run_sync("x")
        assert calls["n"] == expected

    def test_recovers_when_model_fixes_itself(self):
        """先违规后合规：2 次调用自动恢复。这是重试真正的价值。"""
        agent, calls, counters = make_agent(Tier.T2, [BAD, GOOD], retries=2)
        assert agent.run_sync("x").output == GOOD
        assert calls["n"] == 2
        assert counters.violations == 1

    def test_two_counters_differ_on_exhaustion(self):
        """``violations`` 与 ``retries`` 必须分开记。

        retries=2 且持续违规时：校验器被调用 3 次，但**真实重试只有 2 次**——
        最后一次失败后预算已耗尽，不再重试。只留自数的那个，会让失败 run 的重试数
        系统性偏移一格，而那恰恰是最需要精确告警的一类 run。
        """
        agent, _, counters = make_agent(Tier.T2, [BAD], retries=2)
        with pytest.raises(UnexpectedModelBehavior):
            agent.run_sync("x")
        assert counters.violations == 3
        assert counters.retries == 2

    def test_strict_is_explicitly_false(self):
        """T2 必须显式 ``strict=False``。

        不写的话 pydantic-ai 会按 model profile 把 strict 推断成 true 发上去，
        于是 T2 在 OpenAI 系模型上同样承担对齐税——而"不承担对齐税"正是 T2 存在的
        理由，前端显示的保证等级也就成了假的。
        """
        spec = output_spec(Tier.T2, SCHEMA, max_retries=2)
        assert spec.strict is False


# =============================================================================
# T3：明确不校验
# =============================================================================


class TestT3:
    def test_this_project_registers_no_validator(self):
        """断言的是**本项目的策略**，不是框架行为。

        实测四种输出模式下 output_validator 被调用的次数逐位相同——档位差异完全由
        attach_validator 制造。断言"框架不校验"的话，任何人把 T3 误传成 T2 都测不出来。
        """
        agent, _, counters = make_agent(Tier.T3, [BAD])
        assert agent._output_validators == []
        assert counters.violations == 0

    def test_violations_pass_through_in_one_call(self):
        agent, calls, _ = make_agent(Tier.T3, [BAD])
        assert agent.run_sync("x").output == BAD
        assert calls["n"] == 1


# =============================================================================
# 判档
# =============================================================================


class TestTierResolution:
    def test_no_schema_means_t3(self):
        assert resolve_tier(None, has_schema=False, native_ok=True).tier is Tier.T3

    def test_t1_downgrades_when_model_lacks_native_support(self):
        """未知或不支持的模型一律降级。

        宁可多花点重试成本，也不能对用户谎称"有原生保证"。
        """
        choice = resolve_tier(Tier.T1, has_schema=True, native_ok=False)
        assert choice.tier is Tier.T2
        assert choice.downgraded_from is Tier.T1
        assert "降级" in choice.reason

    def test_t1_kept_when_supported(self):
        assert resolve_tier(Tier.T1, has_schema=True, native_ok=True).tier is Tier.T1

    def test_default_is_t2(self):
        """不指定时用 T2：它是唯一在所有模型上都成立的强保证。"""
        assert resolve_tier(None, has_schema=True, native_ok=True).tier is Tier.T2

    def test_form_only_offers_implemented_tiers(self):
        """T1 需要先有"可选字段会被提升为必填"的表单提示才能安全开放。"""
        assert set(AVAILABLE_TIERS) == {Tier.T2, Tier.T3}

    def test_every_tier_has_a_cost_description(self):
        """四档的代价必须都写出来——星槎的价值不是替用户选最强档，
        而是把权衡摆出来并标注代价。"""
        for tier in Tier:
            info = TIER_INFO[tier]
            assert info["shape"] and info["content"] and info["cost"]


class TestT1SchemaRewrite:
    def test_reports_fields_that_become_required(self):
        """``strict=True`` 会把可选字段静默塞进 required（实测）。

        用户在表单里标为可选的字段，在 T1 档下会变成模型**必须**输出的字段——
        这对用户构成隐性违约，表单必须提前说清楚。
        """
        schema = validate_schema(
            {
                "type": "object",
                "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
                "required": ["a"],
            }
        )
        assert t1_rewrites_schema(schema) == ["b"]

    def test_nothing_to_report_when_all_required(self):
        assert t1_rewrites_schema(SCHEMA) == ["tags"]


# =============================================================================
# 护栏
# =============================================================================


class TestGuardrails:
    def test_request_limit_leaves_room_for_tool_roundtrips(self):
        """``request_limit`` 计的是**所有**模型请求，工具往返也算。

        按 ``max_retries + 1`` 算的话，任何带工具的 Agent 都会在第一次工具调用后
        被打断，而且用户看到的是"限流"而不是真实原因。
        """
        limits = limits_for(max_retries=2, max_tool_steps=8, max_tokens=1000, max_cost_usd=None)
        assert limits.request_limit == 3 + 8

    def test_token_limit_is_always_set(self):
        """``cost_limit`` 对 genai-prices 认不出的模型静默失效（实测约三分之一的
        在售模型查不到价），所以 token 上限必须作为永远可执行的硬兜底。"""
        limits = limits_for(max_retries=2, max_tool_steps=4, max_tokens=123456, max_cost_usd=None)
        assert limits.total_tokens_limit == 123456

    def test_cost_limit_is_decimal(self):
        from decimal import Decimal

        limits = limits_for(
            max_retries=1, max_tool_steps=1, max_tokens=10, max_cost_usd=Decimal("0.05")
        )
        assert isinstance(limits.cost_limit, Decimal)


# =============================================================================
# 上游 API 的常驻回归
# =============================================================================


def test_from_spec_accepts_output_type():
    """两个探测组在这一点上结论相反，而选错就要把 guarantee 整块重做。

    这条常驻：pydantic-ai 一旦改掉它，CI 立刻变红，而不是等到线上出现
    "合法输出被判违规"这种极难定位的症状。
    """
    import inspect

    assert "output_type" in inspect.signature(Agent.from_spec).parameters

    from pydantic_ai.models.test import TestModel

    agent = Agent.from_spec(
        {"model": "test", "output_schema": SCHEMA},
        model=TestModel(),
        output_type=output_spec(Tier.T2, SCHEMA, max_retries=1),
    )
    # 显式传的必须**覆盖** spec 里的 output_schema
    assert agent.output_type is not str
    assert type(agent.output_type).__name__ == "ToolOutput"
