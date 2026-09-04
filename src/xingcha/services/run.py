"""Agent 的执行编排。

一次 Agent 调用的生命周期：

    解析 slug → 取（或建）运行时 → 转换 messages → run → 转成 OpenAI 响应

**运行时按 ``(agent_id, version)`` 缓存。** 版本不可变，所以编辑 Agent 会产生新版本号、
旧条目自然不再命中——不需要任何失效逻辑。缓存失效是这类系统最容易出错的地方，
用不可变版本把它整个绕过去。

并发上限收在**进程级**的一个 limiter 上，而不是传 int 给每个 Agent：
``max_concurrency`` 的信号量是**每个 Agent 实例私有**的（实测两个各限 1 的 Agent
全局峰值是 2，传同一个 ConcurrencyLimit 配置对象也不共享）。按 Agent 传 int 等于
完全不封顶。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from pydantic_ai.exceptions import ModelAPIError, UnexpectedModelBehavior, UsageLimitExceeded

from .. import contract as C
from ..contract import Tier
from ..core import builder, guarantee
from ..core.builder import AgentRuntime, BuildOptions
from ..core.guarantee import guard_counters
from ..errors import (
    ModelInvalid,
    QuotaExceeded,
    RequestTimeout,
    SchemaViolation,
    UpstreamError,
    UpstreamTimeout,
)
from .agent import ResolvedAgent

log = logging.getLogger(__name__)


# =============================================================================
# 运行时缓存
# =============================================================================


class RuntimeCache:
    """按 ``(agent_id, version)`` 缓存构造好的 Agent。

    LRU 有界：Agent 实例持有 model 与 provider 引用，无界缓存在版本迭代频繁时会
    慢慢吃掉内存，而这台机器只有 1GB。
    """

    def __init__(self, *, max_entries: int = 64) -> None:
        self._max = max_entries
        self._items: OrderedDict[tuple[int, int], AgentRuntime] = OrderedDict()

    def get(self, key: tuple[int, int]) -> AgentRuntime | None:
        rt = self._items.get(key)
        if rt is not None:
            self._items.move_to_end(key)
        return rt

    def put(self, key: tuple[int, int], rt: AgentRuntime) -> None:
        self._items[key] = rt
        self._items.move_to_end(key)
        while len(self._items) > self._max:
            self._items.popitem(last=False)

    def clear(self) -> None:
        """只在上游配置变化时调用——那会让所有缓存里的 provider 失效。"""
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)


# =============================================================================
# 执行结果
# =============================================================================


@dataclass
class RunOutcome:
    """一次 Agent 运行的结果。

    **不要直接把 ``AgentRunResult`` 交给响应转换函数**：它上面没有
    ``cost_usd`` / ``tier`` / ``schema_retries``（实测 hasattr 全是 False），
    那样写出来的是一个只在运行时才炸的静默 AttributeError。
    """

    output: Any
    model_id: str
    tier: Tier
    is_structured: bool
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    requests: int = 0
    tool_calls: int = 0
    schema_violations: int = 0
    schema_retries: int = 0
    cost_usd: Decimal | None = None
    cost_source: str = C.CostSource.UNKNOWN.value
    extra: dict[str, Any] = field(default_factory=dict)
    #: 本次运行里所有上游响应的 id。用于向 CostSink 取回真实费用。
    #:
    #: 一次运行可能有多次上游调用（schema 重试、工具往返、两阶段），所以是列表——
    #: 只取最后一个会漏掉重试那几次的费用，而那恰好是最贵的情形。
    response_ids: list[str] = field(default_factory=list)

    @property
    def content(self) -> str:
        """``message.content`` **永远是字符串**（契约 §3.6）。

        结构化输出是 ``json.dumps`` 之后的 JSON 文本，调用方 ``json.loads`` 取回。
        把 dict 直接放进 content 会让所有按 str 处理它的客户端崩掉。
        """
        if isinstance(self.output, str):
            return self.output
        return json.dumps(self.output, ensure_ascii=False)


# =============================================================================
# 消息转换
# =============================================================================


def to_prompt(messages: list[dict[str, Any]]) -> tuple[str, str | None]:
    """OpenAI messages → ``(user_prompt, extra_instructions)``。

    v0.2 只支持字符串 content 与 text parts。多模态（image_url / input_audio / file）
    在后续版本——**不静默丢弃**，遇到就明确报错：静默丢掉一张图片会让调用方以为
    模型看到了它。

    ``system`` / ``developer`` 消息合并进 instructions **之后**，不覆盖 Agent 自身的
    指令：Agent 的指令是管理员配置的资产，调用方不该能改写它。
    """
    system_parts: list[str] = []
    convo: list[str] = []

    for m in messages:
        role = m.get("role")
        content = m.get("content")

        if isinstance(content, list):
            texts = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    texts.append(str(part.get("text", "")))
                else:
                    raise ModelInvalid(
                        f"暂不支持 content part 类型 {part.get('type')!r}。"
                        "v0.2 只支持文本；多模态在后续版本。"
                        "（明确报错而不是静默丢弃——否则你会以为模型看到了它。）"
                    )
            content = "\n".join(texts)

        if content is None:
            continue
        if not isinstance(content, str):
            raise ModelInvalid(
                f"message.content 必须是字符串或 parts 数组，收到 {type(content).__name__}"
            )

        if role in ("system", "developer"):
            system_parts.append(content)
        elif role == "assistant":
            convo.append(f"助手：{content}")
        else:
            convo.append(content if len(messages) == 1 else f"用户：{content}")

    if not convo:
        raise ModelInvalid("messages 里没有可用的用户消息")

    return "\n\n".join(convo), ("\n\n".join(system_parts) or None)


# =============================================================================
# 执行
# =============================================================================


async def get_runtime(
    resolved: ResolvedAgent,
    *,
    cache: RuntimeCache,
    provider: Any,
    options: BuildOptions,
    concurrency: Any,
) -> AgentRuntime:
    key = (resolved.agent_id, resolved.version)
    rt = cache.get(key)
    if rt is not None:
        return rt
    rt = builder.build(
        spec_json=resolved.spec_json,
        tier=resolved.tier,
        out_schema=resolved.out_schema,
        provider=provider,
        options=options,
        concurrency=concurrency,
    )
    cache.put(key, rt)
    log.info("已构造 Agent %s v%d（档位 %s）", resolved.slug, resolved.version, rt.tier.value)
    return rt


async def execute(
    rt: AgentRuntime,
    *,
    prompt: str,
    extra_instructions: str | None,
    run_timeout: float,
) -> RunOutcome:
    """跑一次并把异常映射成错误契约。

    整轮墙钟只能靠 ``asyncio.timeout``：``Agent.run`` **没有** timeout 参数
    （实测），per-Agent 超时走 ``model_settings['timeout']``。两种超时来源不同、
    排查路径也不同，所以映射到两个不同的错误码。
    """
    # 计数器随运行时缓存复用，每次运行前归零
    rt.counters.violations = 0
    rt.counters.retries = 0
    rt.counters.provider_noncompliance = 0
    rt.counters.last_error = ""

    kwargs: dict[str, Any] = {"usage_limits": rt.limits}
    if extra_instructions:
        kwargs["instructions"] = extra_instructions

    # 两阶段的用量要**累加**，否则第一步（自由推理，往往是更贵的一步）的 token
    # 完全不进账单——那正好是这一档比 T1 贵一倍的原因所在。
    stage_one: Any = None

    try:
        async with asyncio.timeout(run_timeout):
            if rt.reason_agent is not None:
                # 阶段一：不加任何格式约束，规避对齐税
                stage_one = await rt.reason_agent.run(prompt, **kwargs)
                draft = (
                    stage_one.output
                    if isinstance(stage_one.output, str)
                    else json.dumps(stage_one.output, ensure_ascii=False)
                )
                # 阶段二：只做格式化，此刻才施加约束
                result = await rt.agent.run(guarantee.format_prompt(draft), **kwargs)
            else:
                result = await rt.agent.run(prompt, **kwargs)
    except TimeoutError as e:
        raise RequestTimeout(run_timeout) from e
    except UnexpectedModelBehavior as e:
        # 校验重试耗尽走这里。把最后一次的 schema 错误详情带给调用方——
        # 只说"重试耗尽"没法定位是哪个字段不对。
        if rt.counters.violations:
            guard_counters(rt.counters, tier=rt.tier)
            raise SchemaViolation(rt.counters.last_error, rt.counters.retries) from e
        raise UpstreamError(502, log_detail=f"UnexpectedModelBehavior: {e}") from e
    except UsageLimitExceeded as e:
        # 与 schema 违规分开：混在一起的话，一个 request_limit 设小了的配置错误
        # 会伪装成"模型输出不合规"，查错方向完全反了。
        raise QuotaExceeded("agent", "run", "usage") from e
    except ModelAPIError as e:
        text = str(e)
        if "timed out" in text.lower() or "timeout" in text.lower():
            raise UpstreamTimeout(run_timeout) from e
        raise UpstreamError(502, log_detail=f"ModelAPIError: {e}") from e

    guard_counters(rt.counters, tier=rt.tier)
    usage = result.usage  # 属性，不是方法

    def collect_ids(res: Any) -> list[str]:
        out = []
        for m in res.all_messages():
            rid = getattr(m, "provider_response_id", None)
            if isinstance(rid, str) and rid:
                out.append(rid)
        return out

    response_ids = collect_ids(result)
    if stage_one is not None:
        response_ids = collect_ids(stage_one) + response_ids

    def total(field: str) -> int:
        value = getattr(usage, field, 0) or 0
        if stage_one is not None:
            value += getattr(stage_one.usage, field, 0) or 0
        return value

    return RunOutcome(
        output=result.output,
        model_id=rt.model_id,
        tier=rt.tier,
        is_structured=rt.is_structured,
        input_tokens=total("input_tokens"),
        output_tokens=total("output_tokens"),
        cache_read_tokens=total("cache_read_tokens"),
        requests=total("requests"),
        tool_calls=total("tool_calls"),
        schema_violations=rt.counters.violations,
        schema_retries=rt.counters.retries,
        extra=_extra_usage(usage),
        response_ids=list(dict.fromkeys(response_ids)),
    )


def _extra_usage(usage: Any) -> dict[str, Any]:
    """上游塞进 RunUsage 的额外维度。

    ``RunUsage.__init__`` 接受**任意** kwargs 并 setattr 成动态属性，provider 会借此
    塞新字段（实测 OpenRouter 会塞 ``output_reasoning_tokens``）。上游每加一个维度
    就加一列的话，迁移会没完没了——所以整块存 JSON。
    """
    known = {
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "requests",
        "tool_calls",
        "cost",
        "details",
    }
    out: dict[str, Any] = {}
    for name in dir(usage):
        if name.startswith("_") or name in known:
            continue
        value = getattr(usage, name, None)
        if isinstance(value, int | float | str) and not callable(value):
            out[name] = value
    return out


# =============================================================================
# OpenAI 响应
# =============================================================================


def to_openai_response(outcome: RunOutcome, *, model: str) -> dict[str, Any]:
    """转成 ``chat.completion``。形状进了契约。"""
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": outcome.content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": outcome.input_tokens,
            "completion_tokens": outcome.output_tokens,
            "total_tokens": outcome.input_tokens + outcome.output_tokens,
        },
        C.EXT_KEY: extension_block(outcome),
    }


def extension_block(outcome: RunOutcome, run_id: str | None = None) -> dict[str, Any]:
    """``x_xingcha``。**所有自有字段的唯一落点。**

    金额是**字符串形式的 Decimal 或 null**，不是 number：float 存不住 Decimal，
    而 null（无法定价）必须与真实的 0 费用可区分。
    """
    block: dict[str, Any] = {
        "v": C.EXT_SHAPE_VERSION,
        "tier": outcome.tier.value,
        "cost_usd": str(outcome.cost_usd) if outcome.cost_usd is not None else None,
        "cost_source": outcome.cost_source,
        "schema_violations": outcome.schema_violations,
        "schema_retries": outcome.schema_retries,
    }
    if run_id:
        block["run_id"] = run_id
    return block


def to_sse_frames(outcome: RunOutcome, *, model: str, run_id: str | None = None) -> list[str]:
    """伪流式的帧序列。

    v0.2 只发一个内容帧，但**帧形状与真流式完全一致**——所以 v0.4 换成真 delta 时
    对客户端不可见，只是帧数变多了，而帧数变多是兼容的。

    为什么现在就发伪流式而不是对流式请求返回 400：虽然 400→200 是加法，但客户端
    会为那个 400 **写死绕过逻辑**（探测到就改走非流式），等真流式上线时反而打断它们。
    """
    cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    def frame(payload: dict[str, Any]) -> str:
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    base = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model}

    return [
        frame({**base, "choices": [{"index": 0, "delta": {"role": "assistant"}}]}),
        frame({**base, "choices": [{"index": 0, "delta": {"content": outcome.content}}]}),
        frame({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
        frame(
            {
                **base,
                "choices": [],
                "usage": {
                    "prompt_tokens": outcome.input_tokens,
                    "completion_tokens": outcome.output_tokens,
                    "total_tokens": outcome.input_tokens + outcome.output_tokens,
                },
                C.EXT_KEY: extension_block(outcome, run_id),
            }
        ),
        C.SSE_DONE,
    ]
