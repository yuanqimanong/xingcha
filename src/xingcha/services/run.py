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
import contextlib
import json
import logging
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, ClassVar, Final

from pydantic_ai.exceptions import (
    ModelAPIError,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
    UserError,
)
from pydantic_ai.usage import RunUsage

from .. import contract as C
from ..contract import Tier
from ..core import builder, guarantee
from ..core.builder import AgentRuntime, BuildOptions
from ..core.guarantee import guard_counters
from ..errors import (
    AgentBuildFailed,
    ModelInvalid,
    QuotaExceeded,
    RequestTimeout,
    SchemaViolation,
    UpstreamError,
    UpstreamTimeout,
    XingchaError,
)
from ..obs import tracing as tracing_mod
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
    #: 这次运行的**完整消息链**（``result.all_messages()``），含指令、每一次请求
    #: 与响应、以及校验重试那几轮。后台的「试运行」把它渲染出来。
    #:
    #: 取自上游而不是自己重建：重建出来的"应该发了什么"和真正发出去的东西会分叉，
    #: 而分叉的那一刻恰好是最需要看这个面板的时候。
    messages: list[Any] = field(default_factory=list)

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


#: OpenAI 的 role 里，星槎认得的那几个。
#:
#: ``tool`` / ``function`` 不在其中，而且**明确拒绝**而不是当普通文本收下。
#: 原先它们会落进 else 分支、被当成用户消息拼进去——一段工具返回值被贴上
#: "用户："的标签送给模型，模型会把它当人说的话。工具在星槎里是服务端的能力
#: （capabilities），调用方本来就不该自己回放工具轮。
_ROLES_SYSTEM: Final = ("system", "developer")
_ROLES_KNOWN: Final = (*_ROLES_SYSTEM, "user", "assistant")


@dataclass(frozen=True, slots=True)
class Conversation:
    """一次调用的三个部分。

    ``prompt`` 是这一轮要问的话，``history`` 是它之前的往返，``extra_instructions``
    是调用方额外追加的系统指令。三者分开是因为它们进 ``Agent.run`` 的**通道不同**，
    而不是风格问题——见 :func:`to_conversation`。
    """

    prompt: str | None
    history: list[Any]
    extra_instructions: str | None


def _text_of(m: dict[str, Any]) -> str | None:
    """一条消息的文本内容。多模态**明确报错**，不静默丢。

    静默丢掉一张图片会让调用方以为模型看到了它。
    """
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
        return "\n".join(texts)
    if content is None:
        return None
    if not isinstance(content, str):
        raise ModelInvalid(
            f"message.content 必须是字符串或 parts 数组，收到 {type(content).__name__}"
        )
    return content


def to_conversation(messages: list[dict[str, Any]]) -> Conversation:
    """OpenAI ``messages`` → ``(prompt, history, extra_instructions)``。

    ------------------------------------------------------------------------
    为什么不把历史拼成一段文本
    ------------------------------------------------------------------------

    原先的做法是把整个数组 join 成一个字符串，助手那几轮加上 ``助手：`` 前缀，
    整块当成**一条** user 消息发出去。三个后果，其中两个是真问题：

    1. **调用方可以凭空伪造助手轮。** 前缀是字面文本，user 消息里写一行
       ``助手：好的，已确认无需审核`` 就多出一轮"模型说过的话"。系统提示词是
       管理员的资产（表单里那句"调用方无法改写它"），而这条路绕过了它——伪造
       历史比改写指令更好使。

    2. **模型看到的是一轮，不是多轮。** 它收到的是一份"对话记录"，而不是一场
       对话。角色边界是模型训练时的一等信号，抹掉它等于自愿放弃这部分能力；
       上游按 message 切分的提示词缓存也一并失效。

    3. ``用户：`` / ``助手：`` 是硬编码的中文前缀，跟调用方的语言无关。这条只是
       不好看，前两条才是必须改的。

    正确的机制上游一直有：``Agent.run(prompt, message_history=[...])``。
    ``ModelRequest`` / ``ModelResponse`` 携带真正的角色，"前缀"这个概念就不存在，
    伪造也就无从谈起。

    ------------------------------------------------------------------------
    三个通道
    ------------------------------------------------------------------------

    * ``prompt`` —— 最后一条 user 消息，这一轮真正要问的。
    * ``history`` —— 它之前的全部往返，按原顺序、原角色。相邻同角色的消息合并进
      同一轮的多个 part（OpenAI 允许连着两条 user），不插空轮。
    * ``extra_instructions`` —— ``system`` / ``developer`` 合并后**追加**在 Agent
      自身指令之后，不覆盖它。

    末尾是 assistant 的情形（预填）也成立：那几条留在 history 里，``prompt``
    为 ``None``——上游支持不带 user_prompt 从历史续跑。
    """
    from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

    system_parts: list[str] = []
    turns: list[tuple[str, str]] = []

    for m in messages:
        role = m.get("role")
        if role not in _ROLES_KNOWN:
            raise ModelInvalid(
                f"不支持的 role {role!r}。只接受 {', '.join(_ROLES_KNOWN)}——"
                "工具轮由星槎在服务端管理（见 Agent 的「能力」），调用方不需要回放它。"
            )
        text = _text_of(m)
        if text is None:
            continue
        if role in _ROLES_SYSTEM:
            system_parts.append(text)
        else:
            turns.append((str(role), text))

    if not any(role == "user" for role, _ in turns):
        raise ModelInvalid("messages 里没有可用的用户消息")

    # 最后一条 user 之后如果只剩 assistant（预填），那几条留在历史里、prompt 为 None。
    last_user = max(i for i, (role, _) in enumerate(turns) if role == "user")
    prompt: str | None = None
    if last_user == len(turns) - 1:
        prompt = turns.pop()[1]

    # 相邻同角色合并成一轮里的多个 part：OpenAI 允许连着两条 user，而让每条各起
    # 一轮，就得在中间插入一个模型没说过的空响应轮。
    history: list[Any] = []
    for role, text in turns:
        part: Any = UserPromptPart(content=text) if role == "user" else TextPart(content=text)
        want = ModelRequest if role == "user" else ModelResponse
        if history and isinstance(history[-1], want):
            history[-1].parts.append(part)
        else:
            history.append(want(parts=[part]))

    return Conversation(
        prompt=prompt,
        history=history,
        extra_instructions="\n\n".join(system_parts) or None,
    )


def apply_prompting(conv: Conversation, prompting: builder.Prompting) -> Conversation:
    """把 Agent 自己的用户模板与少样本示例套进这次调用。

    分成两步（``to_conversation`` 只做 OpenAI → 内部形状，这里才做 Agent 相关的
    加工）不是洁癖：前者的报错是"你的 messages 不对"，必须在配额占名额**之前**
    发生；后者需要先取到运行时才知道模板是什么。合成一步就得二选一。

    **模板套在每一条 user 消息上，包括历史里的。**

    只套当前这一条的话，历史里那几轮就和模型当时实际看到的不一样了——上一轮它
    收到的是 ``请抽取以下合同：<A>``，这一轮回放给它的却是光秃秃的 ``<A>``。
    模型会觉得自己上次是在回答另一个问题。

    示例排在调用方历史**之前**：它们是"开场前的演示"，不是对话的一部分。
    """
    if prompting.is_empty:
        return conv

    from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

    def framed(text: str) -> str:
        t = prompting.user_template
        return t.replace(builder.PROMPT_PLACEHOLDER, text) if t else text

    history: list[Any] = [
        m
        for e in prompting.examples
        for m in (
            ModelRequest(parts=[UserPromptPart(content=framed(e.user))]),
            ModelResponse(parts=[TextPart(content=e.assistant)]),
        )
    ]
    for msg in conv.history:
        if isinstance(msg, ModelRequest):
            history.append(
                ModelRequest(
                    parts=[
                        UserPromptPart(content=framed(part.content))
                        if isinstance(part, UserPromptPart) and isinstance(part.content, str)
                        else part
                        for part in msg.parts
                    ]
                )
            )
        else:
            history.append(msg)

    return Conversation(
        prompt=framed(conv.prompt) if conv.prompt is not None else None,
        history=history,
        extra_instructions=conv.extra_instructions,
    )


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
    conv: Conversation,
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

    # 两阶段的用量要**累加**，否则第一步（自由推理，往往是更贵的一步）的 token
    # 完全不进账单——那正好是这一档比 T1 贵一倍的原因所在。
    stage_one: Any = None

    # 用量累加器。**必须传，而且必须在 try 外面建。**
    #
    # 重试耗尽时 ``Agent.run`` 抛异常，手上没有任何 result 可读——原先的失败路径
    # 因此把 token 记成 0、费用记成 None。一次 retries=2 的失败打了 **3 次**上游，
    # 账上却是 0：账单少报的恰好是最贵的一类调用，而金额配额结算 None 意味着
    # **这条最贵的路径完全不占金额配额**（钱刹车没落在要刹的地方）。
    #
    # pydantic-ai 会原地累加进这个对象（实测：3 次调用后 input=36 output=18
    # requests=3，与成功路径的口径一致），所以异常抛出后它仍然是完整的。
    usage_acc = RunUsage()

    with map_errors(rt, run_timeout, usage=usage_acc):
        async with asyncio.timeout(run_timeout):
            kwargs = run_kwargs(rt, conv.extra_instructions, usage_acc)
            if rt.reason_agent is not None:
                # 阶段一：不加任何格式约束，规避对齐税
                stage_one = await rt.reason_agent.run(
                    conv.prompt, message_history=conv.history, **kwargs
                )
                draft = (
                    stage_one.output
                    if isinstance(stage_one.output, str)
                    else json.dumps(stage_one.output, ensure_ascii=False)
                )
                # 阶段二**不带历史**：它是对上一步草稿的纯格式化，把对话再塞一遍
                # 只会让模型有机会顺着对话继续答，而不是照着 schema 重排。
                result = await rt.agent.run(guarantee.format_prompt(draft), **kwargs)
            else:
                result = await rt.agent.run(conv.prompt, message_history=conv.history, **kwargs)

    guard_counters(rt.counters, tier=rt.tier)
    return outcome_from(rt, result, stage_one=stage_one)


def run_kwargs(
    rt: AgentRuntime, extra_instructions: str | None, usage: Any = None
) -> dict[str, Any]:
    """给 ``Agent.run`` / ``run_stream`` 的公共 kwargs。

    ``usage`` 是一个 :class:`RunUsage` 累加器，pydantic-ai 会**原地累加**进去。
    传它的理由见 :func:`execute`——异常路径上没有 result 可读，累加器是唯一还
    拿得到用量的东西。两阶段（T1P）也传同一个，所以第一阶段的 token 不会丢。
    """
    kwargs: dict[str, Any] = {"usage_limits": rt.limits}
    if extra_instructions:
        kwargs["instructions"] = extra_instructions
    if usage is not None:
        kwargs["usage"] = usage
    return kwargs


@contextlib.contextmanager
def map_errors(rt: AgentRuntime, run_timeout: float, usage: Any = None) -> Iterator[None]:
    """把 pydantic-ai 的异常映射成错误契约。

    命令式与流式**共用这一份**。写两份的话，两条路径迟早在"同一个上游故障返回
    不同错误码"上分叉——而调用方是按错误码写重试逻辑的。

    注意它必须包在 ``asyncio.timeout`` **外面**：整轮超时是由 timeout 的 ``__aexit__``
    抛出的，放在里面看不到。

    ``usage`` 是那个原地累加的累加器。失败路径上它是唯一还拿得到用量的东西，所以
    这里把它挂到抛出去的 :class:`XingchaError` 上——契约冻结了"429 / 422 也带
    usage"（``USAGE_ON_ERROR``），而没有这一步，那句承诺是假的。
    """

    def tag(err: XingchaError) -> XingchaError:
        if usage is not None:
            err.usage = usage
        return err

    try:
        yield
    except TimeoutError as e:
        raise tag(RequestTimeout(run_timeout)) from e
    except UnexpectedModelBehavior as e:
        # 校验重试耗尽走这里。把最后一次的 schema 错误详情带给调用方——
        # 只说"重试耗尽"没法定位是哪个字段不对。
        if rt.counters.violations:
            guard_counters(rt.counters, tier=rt.tier)
            raise tag(SchemaViolation(rt.counters.last_error, rt.counters.retries)) from e
        raise tag(UpstreamError(502, log_detail=f"UnexpectedModelBehavior: {e}")) from e
    except UserError as e:
        # 上游在**请求时**才拒的配置问题：能力与这个 model / 这条 API 不兼容之类。
        # 不接住的话它一路冒到最外层变成"服务内部错误"，而这类失败每次都发生、
        # 原因还写得很具体（"WebSearchTool is not supported with OpenAIChatModel"）。
        raise tag(AgentBuildFailed(f"UserError: {e}", reason=str(e))) from e
    except UsageLimitExceeded as e:
        # 与 schema 违规分开：混在一起的话，一个 request_limit 设小了的配置错误
        # 会伪装成"模型输出不合规"，查错方向完全反了。
        raise tag(QuotaExceeded("agent", "run", "usage")) from e
    except ModelAPIError as e:
        text = str(e)
        if "timed out" in text.lower() or "timeout" in text.lower():
            raise tag(UpstreamTimeout(run_timeout)) from e
        raise tag(
            UpstreamError(502, log_detail=f"ModelAPIError: {e}", upstream_message=_upstream_says(e))
        ) from e


def _upstream_says(e: Any) -> str | None:
    """从 ``ModelAPIError`` 里挖出上游自己写的那句话。

    ``str(e)`` 是 ``status_code: 400, model_name: x, body: {...}`` 这种拼装串，
    整条回显给调用方既啰嗦又会把 model_name 之类的内部细节漏出去。这里只取
    body 里的 ``message``；结构不认识就返回 None，宁可少说也不说错。
    """
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        for key in ("message", "error"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value
            if isinstance(value, dict) and isinstance(value.get("message"), str):
                return value["message"]
    return None


#: "没传" 与 "传了 None" 要能区分——流式的正文可以是空字符串。
_UNSET: Any = object()


def stream_finish_reason(result: Any) -> str | None:
    """流式结束时上游给出的结束原因。``None`` 表示**没给**。

    这个函数存在的唯一理由是：上游在流中途挂掉时 httpx 与 pydantic-ai
    **一个异常都不抛**（实测：连接断了，``stream_text`` 的迭代静默结束，
    ``is_complete`` 照样是 True）。除了"最后那条 ModelResponse 有没有
    finish_reason"，没有别的判据。
    """
    reason = getattr(getattr(result, "response", None), "finish_reason", None)
    return str(reason) if reason else None


def _collect_ids(res: Any) -> list[str]:
    out = []
    for m in res.all_messages():
        rid = getattr(m, "provider_response_id", None)
        if isinstance(rid, str) and rid:
            out.append(rid)
    return out


def outcome_from(
    rt: AgentRuntime, result: Any, *, stage_one: Any = None, output: Any = _UNSET
) -> RunOutcome:
    """从 pydantic-ai 的运行结果拼出本项目的用量口径。

    命令式结果与流式结果**都能进这里**：``StreamedRunResult`` 同样有 ``usage``
    与 ``all_messages()``。两条路径共用一份口径，账单才能一起 SUM。

    ``output`` 用于流式——``StreamedRunResult`` 没有 ``output`` 属性，正文得由调用方
    把 delta 攒起来传进来。
    """
    usage = result.usage  # 属性，不是方法

    response_ids = _collect_ids(result)
    if stage_one is not None:
        response_ids = _collect_ids(stage_one) + response_ids

    def total(field: str) -> int:
        value = getattr(usage, field, 0) or 0
        if stage_one is not None:
            value += getattr(stage_one.usage, field, 0) or 0
        return value

    return RunOutcome(
        output=result.output if output is _UNSET else output,
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
        messages=_all_messages(stage_one) + _all_messages(result),
    )


def _all_messages(result: Any) -> list[Any]:
    """完整消息链。拿不到就空——它只喂给后台面板，不该让一次调用因此失败。"""
    try:
        return list(result.all_messages()) if result is not None else []
    except Exception:  # pragma: no cover - 上游换 API 时退化成"面板空着"
        return []


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


def to_openai_response(
    outcome: RunOutcome, *, model: str, run_id: str | None = None
) -> dict[str, Any]:
    """转成 ``chat.completion``。形状进了契约。

    ``run_id`` 必须带上。契约 §3.6 把它列进 ``x_xingcha``，而 5xx 的固定文案就是
    「请把 run_id 提供给管理员」——**成功的响应里没有它的话，"这次回答不对，去查一下"
    根本无从下手**：那正是最需要查的一类调用（200 但结果可疑），而它偏偏没有抓手。
    """
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
        C.EXT_KEY: extension_block(outcome, run_id),
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


class SSEFrames:
    """一次流式响应的帧工厂。

    **帧形状是契约冻结的**（见 CONTRACT §3.7），所以它只能有一个来源。真流式与
    伪流式都从这里取帧——两条路径各写一份的话，"把伪流式升级成真 delta 对客户端
    不可见"这个承诺就没有任何东西在守。

    ``id`` / ``created`` 在同一次响应的所有帧里必须一致，所以它们是实例状态而不是
    每帧现算。
    """

    __slots__ = ("_base",)

    def __init__(self, *, model: str) -> None:
        self._base = {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
        }

    def _frame(self, payload: dict[str, Any]) -> str:
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def role(self) -> str:
        return self._frame(
            {**self._base, "choices": [{"index": 0, "delta": {"role": "assistant"}}]}
        )

    def content(self, text: str) -> str:
        return self._frame({**self._base, "choices": [{"index": 0, "delta": {"content": text}}]})

    #: pydantic-ai 的结束原因 → OpenAI 的取值。两边词表不完全一样，而客户端只认
    #: OpenAI 那套（尤其 ``length``：那是"答案被 max_tokens 砍了"的唯一信号）。
    FINISH_REASONS: ClassVar[dict[str, str]] = {
        "stop": "stop",
        "length": "length",
        "content_filter": "content_filter",
        "tool_call": "tool_calls",
    }

    def finish(self, reason: str = "stop") -> str:
        return self._frame(
            {**self._base, "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}
        )

    def summary(self, outcome: RunOutcome, run_id: str | None) -> str:
        return self._frame(
            {
                **self._base,
                "choices": [],
                "usage": {
                    "prompt_tokens": outcome.input_tokens,
                    "completion_tokens": outcome.output_tokens,
                    "total_tokens": outcome.input_tokens + outcome.output_tokens,
                },
                C.EXT_KEY: extension_block(outcome, run_id),
            }
        )


def to_sse_frames(outcome: RunOutcome, *, model: str, run_id: str | None = None) -> list[str]:
    """伪流式的帧序列：一次性把已有结果切成合法的帧序列。

    结构化 Agent 对 ``stream=true`` 直接 400，所以走到这里的只有**真流式失败后的
    回落**与纯文本以外的情形。纯文本 Agent 自 v0.4 起走真 delta。

    为什么当初就发伪流式而不是对流式请求返回 400：虽然 400→200 是加法，但客户端
    会为那个 400 **写死绕过逻辑**（探测到就改走非流式），等真流式上线时反而打断它们。
    """
    f = SSEFrames(model=model)
    return [
        f.role(),
        f.content(outcome.content),
        f.finish(),
        f.summary(outcome, run_id),
        C.SSE_DONE,
    ]


# =============================================================================
# 真流式
# =============================================================================


async def stream_frames(
    rt: AgentRuntime,
    *,
    conv: Conversation,
    run_timeout: float,
    model: str,
    run_id: str | None,
    debounce: float | None,
    on_outcome: Callable[[RunOutcome, Exception | None], Awaitable[None]],
    tracing: Any = None,
) -> AsyncGenerator[str, None]:
    """真流式：一边收 delta 一边发帧。

    ------------------------------------------------------------------------
    为什么整条生命周期在同一个生成器里
    ------------------------------------------------------------------------

    流式的用量只有在流**结束之后**才知道，而 run 记录与配额结算都要用它。把
    "开流—发帧—收尾结算"拆到 API 层去编排，就得把一个未关闭的 async CM 跨越响应
    边界传出去；一旦客户端中途断开，那个 CM 由谁关就成了说不清的事。放在一个词法
    作用域里，``finally`` 就是答案。

    ``on_outcome(outcome, aborted)`` 是给调用方结算的钩子（落库 + 结算配额）。它在
    **汇总帧之前**被 await，所以汇总帧里的费用是已经落定的值，而不是一个稍后才成立
    的承诺。中途失败时 ``aborted`` 非 None，但**照样要结算**——流到一半的调用一样
    花了钱。客户端提前断开也一样：结算在 ``finally`` 里，那条路径同样会走到。

    整轮墙钟仍由 ``asyncio.timeout`` 兜（与命令式同一个口径）。代价是它可能在生成器
    挂在 ``yield`` 上时触发，此时取消落在正在 ``send`` 的那一侧，连接直接断——对一次
    超时来说这正是诚实的表现：客户端收到一个没有 ``[DONE]`` 的截断流。

    ------------------------------------------------------------------------
    第一帧为什么要 eager 拉
    ------------------------------------------------------------------------

    ``run_stream()`` 的 ``__aenter__`` 会真的发出请求（实测上游拒连时它立刻抛）。
    调用方应当在构造 ``StreamingResponse`` **之前**先 ``anext()`` 一次：那一刻状态码
    还没提交，上游故障还能变成一个正常的 502 JSON；等 200 发出去之后就只能靠
    "流没有以 [DONE] 结尾"来表达失败了。
    """
    frames = SSEFrames(model=model)
    chunks: list[str] = []
    result: Any = None
    outcome: RunOutcome | None = None
    aborted: Exception | None = None
    finish_reason = "stop"
    settled = False

    async def settle() -> RunOutcome | None:
        """结算一次，且只结算一次。

        放在 ``finally`` 里调，因为**客户端中途断开**这条路径既不走正常收尾也不走
        异常收尾：生成器被 ``aclose()`` 掉，``yield`` 处抛出 GeneratorExit。不在这里
        结算的话，那次调用连 run 行都不会有——上游的钱花了，账上一片空白。
        """
        nonlocal settled, outcome
        if settled:
            return outcome
        settled = True
        if result is None:  # 连流都没开起来，交给调用方的错误路径去记
            return None
        outcome = outcome_from(rt, result, output="".join(chunks))
        await on_outcome(outcome, aborted)
        # 在 span 还开着的时候写：外层的 with 持有它，而这个 finally 在那个 with 内。
        # 反过来的话 set_attribute 是**静默的空操作**——span 上什么都没有，
        # 而代码看起来一切正常。
        tracing_mod.record_outcome(
            span,
            status=C.RunStatus.UPSTREAM_ERROR.value if aborted else C.RunStatus.OK.value,
            error_type=C.ErrorType.UPSTREAM_ERROR.value if aborted else None,
            tier=rt.tier.value,
            cost_usd=outcome.cost_usd,
            cost_source=outcome.cost_source,
        )
        return outcome

    # span 必须包住整条生命周期，所以只能在这里开——它是唯一同时看得见"开流"与
    # "收尾"的词法作用域。在 API 层包 StreamingResponse 的话，span 会在第一帧发出去
    # 时就关掉，之后所有 delta 与最终的费用都落在 span 外面。
    #
    # 它在 try 的**外面**：结算要在 span 还开着的时候发生，否则 set_attribute 是
    # 静默的空操作——span 上什么都没有，而代码看起来一切正常。
    with tracing_mod.run_span(tracing, kind="agent", run_id=run_id or "", model=model) as span:
        try:
            with map_errors(rt, run_timeout):
                async with asyncio.timeout(run_timeout):
                    kwargs = run_kwargs(rt, conv.extra_instructions)
                    async with rt.agent.run_stream(
                        conv.prompt, message_history=conv.history, **kwargs
                    ) as stream:
                        result = stream
                        yield frames.role()
                        try:
                            async for delta in stream.stream_text(delta=True, debounce_by=debounce):
                                # 空 delta 不发帧：有些上游会吐空字符串心跳，转发出去
                                # 只会让客户端多解析几个无意义的帧。
                                if delta:
                                    chunks.append(delta)
                                    yield frames.content(delta)
                        except (ModelAPIError, UnexpectedModelBehavior) as e:
                            aborted = e
                        else:
                            reason = stream_finish_reason(stream)
                            if reason is None:
                                # **没给 finish_reason 就判截断，不判成功。**
                                #
                                # 判成功的话，一次被砍掉一半的回答会带着
                                # ``finish_reason: "stop"`` 和 ``[DONE]`` 交到客户端
                                # 手上，它连察觉的机会都没有——静默的数据损坏比一个
                                # 可检测的失败信号糟得多。
                                #
                                # 代价：真有中转不发 finish_reason 的话，它的流式在
                                # 这里会一律被判失败。那种情况该修中转，或别用流式。
                                aborted = UpstreamError(
                                    502,
                                    log_detail="上游流没有给出 finish_reason，无法确认完整",
                                )
                            else:
                                finish_reason = SSEFrames.FINISH_REASONS.get(reason, "stop")
        finally:
            await settle()

        guard_counters(rt.counters, tier=rt.tier)
        assert outcome is not None

        if aborted is not None:
            # 200 已经发出去了，状态码改不了。**不发 [DONE]** 就是给客户端的失败
            # 信号——OpenAI 自己也是这个行为，客户端按"流没有以 [DONE] 结尾"判失败。
            #
            # 这里 return 而不是 raise：抛出去只会在 ASGI 层变成一个没人处理的异常，
            # 客户端看到的字节完全一样，日志却多一堆噪音。花费已由 settle 记下了。
            log.warning("流式中途失败，不发 [DONE]：%s", aborted)
            return

        yield frames.finish(finish_reason)
        yield frames.summary(outcome, run_id)
        yield C.SSE_DONE
