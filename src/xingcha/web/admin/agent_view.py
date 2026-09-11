"""Agent 表单的取值与展示。

和路由分开：这里全是**纯函数**（表单 dict → spec、spec → 模板上下文），
不碰请求也不碰数据库，可以单独测。
"""

from __future__ import annotations

import json
import logging
from itertools import zip_longest
from types import SimpleNamespace
from typing import Any

from fastapi import Request

from ...core import builder
from ...core import guarantee as guarantee_mod
from ...core.guarantee import AVAILABLE_TIERS, TIER_INFO
from ...core.ids import new_agent_slug
from ...core.schema_guard import SchemaRejected, validate_schema
from ...core.schema_lint import lint, summarize
from ...services import agent as agent_svc
from ...services import agent_test as test_svc
from .render import fmt_cost, fmt_time

log = logging.getLogger(__name__)


def tier_options() -> list[Any]:
    """表单里可选的档位。

    只列已实现的：T1 需要先有"strict=True 会静默把可选字段提升为必填"的提示，
    没有它就开放 T1 等于让用户在不知情的情况下承担对齐税。
    """

    return [SimpleNamespace(value=t.value, **TIER_INFO[t]) for t in AVAILABLE_TIERS]


def prompting_from_form(raw: Any) -> Any:
    """表单 → :class:`builder.Prompting`。校验在 builder 里，这里只负责取值。

    示例是不定组数的，所以按 ``getlist`` 收而不是逐个声明 Form 参数——组数由前端
    决定，后端写死几组就等于给"再加一组"设了一个看不见的上限。
    """

    users = raw.getlist("ex_user")
    assistants = raw.getlist("ex_assistant")
    # 两个列表按位置配对。长度不等只可能是前端出了 bug，短的那边补空串，
    # 让 validate_prompting 报"要成对"，而不是在这里 IndexError。
    pairs = [
        builder.Example(u, a)
        for u, a in zip_longest(users, assistants, fillvalue="")
        if (u or a).strip()
    ]
    return builder.validate_prompting(
        str(raw.get("user_template") or ""), pairs, str(raw.get("output_channel") or "tool")
    )


def lint_ctx(schema_text: str, tier: str) -> dict[str, Any]:

    if not schema_text.strip():
        return {"hints": [], "summary": "没有定义 schema，按纯文本输出。", "has_warn": False}
    try:
        inlined = validate_schema(schema_text)
    except SchemaRejected as e:
        return {
            "hints": [SimpleNamespace(path="", level="warn", message=str(e))],
            "summary": "schema 不被接受",
            "has_warn": True,
        }
    hints = lint(inlined, tier_is_native=tier == "T1")
    return {
        "hints": hints,
        "summary": summarize(hints),
        "has_warn": any(h.level == "warn" for h in hints),
    }


def active_upstream_label(state: Any) -> str:
    """当前出口的名字，用在"换过上游所以有模型失效了"那条提示里。

    只用于文案。取不到就说"当前上游"——为了一句提示去多查一次数据库不值得。
    """
    ref = getattr(getattr(state, "upstream", None), "ref", "") or ""
    return str(ref) or "当前上游"


def put_agents_flash(request: Request, session: Any, level: str, message: str) -> None:
    """给 Agent 列表页留一句一次性提示（跨 303 用）。"""
    if session is not None:
        request.app.state.xc.flash.put(f"{session.id}:agents", f"{level}\n{message}")


def take_agents_flash(request: Request, session: Any) -> Any:
    if session is None:
        return None
    raw = request.app.state.xc.flash.take(f"{session.id}:agents")
    if not raw:
        return None
    level, _, message = raw.partition("\n")
    return SimpleNamespace(level=level or "info", title="", message=message)


def empty_form() -> Any:

    return SimpleNamespace(
        # slug 发布后不可改，所以默认值必须一次到位、不会撞。用户可以整个改掉。
        slug=new_agent_slug(),
        name="",
        description="",
        instructions="",
        model="",
        schema="",
        tier="T2",
        retries=2,
        group="",
        **settings_view({}),
    )


async def model_choices(state: Any) -> tuple[list[Any], int]:
    models = state.catalog.all()
    return models, sum(1 for m in models if m.supports_native_schema)


async def form_shell(request: Request) -> dict[str, Any]:
    """三个 handler（new / edit / save 出错回填）共用的表单上下文。

    写三份的话，加一个分区就要改三处——而漏掉一处的症状是"新建页有这个字段、
    编辑页没有"，一种很晚才会被发现的不一致。
    """

    state = request.app.state.xc
    models, native = await model_choices(state)
    tracing = state.tracing
    async with state.sessionmaker() as s:
        groups = await agent_svc.all_groups(s, state.keyring)
    return {
        "models": models,
        "native_count": native,
        "tiers": tier_options(),
        # 传 request_timeout 进去，「timeout 留空是多少」才显示得出真值。
        "model_settings": builder.model_settings_fields(state.settings.request_timeout),
        "choice_settings": builder.choice_settings_fields(),
        "output_channels": guarantee_mod.OUTPUT_CHANNELS,
        "capabilities": builder.form_capabilities(),
        "groups": groups,
        # 分组下拉里"没分组"那一项的显示名。库里存的是 NULL，见 agent_svc.DEFAULT_GROUP。
        "default_group": agent_svc.DEFAULT_GROUP,
        # 可观测那一栏要知道地址配了没：没配就不该给一个勾了没用的开关
        "trace_endpoint": tracing.endpoint if tracing is not None else "",
        "trace_include_content": tracing.include_content if tracing is not None else False,
    }


def settings_view(spec: dict[str, Any]) -> dict[str, Any]:
    """提示词组装/模型参数/能力/可观测 几块的回填值。

    **这里逐项挑键，所以 form_view 新增字段时必须同步。** 漏掉一项不会报错——
    模板里读到的是 Jinja 的 Undefined，渲染成空串，于是编辑页那一栏看起来"从来
    没填过"，一保存就把用户设过的东西清掉。刚在 user_template 上踩过一次。
    """

    view = builder.form_view(spec)
    settings = view["settings"]
    filled = {k: v for k, v in settings.items() if v not in ("", None)}
    return {
        "settings": settings,
        "has_settings": bool(filled),
        "settings_count": len(filled),
        "capabilities": view["capabilities"],
        "instrumented": view["instrumented"],
        "user_template": view["user_template"],
        "examples": view["examples"],
        "output_channel": view["output_channel"],
        # 已保存、但表单不再提供的能力。渲染出来让人自己决定去留——不渲染的话，
        # 下一次保存会把它静默清掉，而用户什么都没动。
        "legacy_capabilities": sorted(
            view["capabilities"] - {n for n, _, _, _ in builder.form_capabilities()}
        ),
    }


def model_report_ctx(request: Request, model: str) -> dict[str, Any]:

    state = request.app.state.xc
    model = model.strip()
    if not model:
        # 空模型名也要给全键：调用方会直接把这份 dict 摊进模板上下文，缺键会 KeyError。
        return {
            "model": "",
            "checks": [],
            "info": None,
            "no_catalog": False,
            "capabilities": [],
            "hidden_caps": [],
            "sampling_ignored": False,
        }
    info = state.catalog.get(model)
    known = info is not None and info.declares_capabilities
    checks = builder.model_report(model, state.provider, info) if state.provider is not None else []
    by_key = {c.key: c for c in checks}

    def usable(cap_key: str) -> bool:
        """这个模型明确说了不支持才算不支持。

        ``unknown`` 按**可用**处理：目录没有能力信息时（厂商直连的 /models 常常
        只回一个 id）把选项藏起来，等于因为"不知道"而把一个明明能用的能力拿走。
        实测 DeepSeek 直连的深度思考就是这种情况——目录一片空白，而它真的会想。
        """
        c = by_key.get(cap_key)
        return c is None or c.state != "no"

    return {
        "model": model,
        "info": info,
        # 目录里根本没有能力信息（厂商直连的 /models 常常只回 id）——那时候满屏
        # 打叉是在撒谎。这一位让模板改说"不知道"。
        "no_catalog": not known,
        "checks": checks,
        # 这个模型用不了的能力，表单里就别摆出来。
        "capabilities": [
            (name, label, hint, args)
            for name, label, hint, args in builder.form_capabilities()
            if usable({"Thinking": "reasoning", "WebSearch": "web_search"}.get(name, name))
        ],
        "hidden_caps": [
            label
            for name, label, _, _ in builder.form_capabilities()
            if not usable({"Thinking": "reasoning", "WebSearch": "web_search"}.get(name, name))
        ],
        # temperature / top_p 会不会被静默丢掉。见 builder.sampling_params_ignored——
        # 那件事只在服务端日志里 warn 一句，页面上不说的话没人会发现。
        "sampling_ignored": builder.sampling_params_ignored(model, state.provider),
    }


def take_saved(request: Request, session: Any) -> Any:
    """取走"刚保存成 vN"的一次性提示。

    保存走的是 POST-redirect-GET，所以结果必须经 flash 带过来——直接在 GET 里写死
    ``None`` 的话，模板里那一块永远不显示，而它同时承载着**判档降级的说明**
    （"你请求了 T1，但这个模型不支持原生约束，已降级到 T2"）。那句话丢了，
    用户会以为自己拿到了 T1 的原生约束，而实际跑的是 T2 的校验后重试。
    """
    if session is None:
        return None
    raw = request.app.state.xc.flash.take(f"{session.id}:saved_agent")
    if not raw:
        return None
    version, _, note = raw.partition("\n")
    return SimpleNamespace(version=version, tier_note=note or "")


def test_history_row(row: Any) -> Any:
    """一条试运行历史的展示形状。"""

    return SimpleNamespace(
        when=fmt_time(row.created_at),
        ok=row.ok,
        model=row.model,
        tier=row.tier or "—",
        input=row.input,
        output=row.output or "",
        error=row.error or "",
        elapsed=f"{(row.elapsed_ms or 0) / 1000:.1f}",
        # 入 / 出分开给模板：合成 "950 → 1596" 一个串的时候，页面上没有任何地方
        # 说得清那个箭头是什么意思，读者只能猜。
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        cost=fmt_cost(row.cost_usd) if row.cost_usd else "—",
        violations=row.violations,
        rows=test_svc.chain_of(row),
    )


def chain_rows(messages: list[Any]) -> list[Any]:
    """``all_messages()`` → 面板上的一行一条。

    渲染的是**上游实际收发的东西**，不是照表单重建的"应该发什么"。两者分叉的
    那一刻正好是最需要看这个面板的时候，所以重建版没有价值。
    """
    rows: list[Any] = []

    # 系统指令**排在最前**，而且只出现一次。
    #
    # 它挂在 ModelRequest 上，而上游只挂在**最后一个**上——照原位渲染的话，它会
    # 夹在少样本示例中间，读起来像是"演示完两轮之后才告诉模型它是谁"。而实际上
    # 它对整轮都生效。单拎出来还有一个好处：看得见 Agent 自己的指令与调用方追加
    # 的那段拼在一起之后长什么样。
    for msg in messages:
        text = getattr(msg, "instructions", None)
        if text:
            rows.append(SimpleNamespace(role="instructions", label="系统指令", text=text, meta=""))
            break

    for msg in messages:
        is_req = getattr(msg, "kind", "") == "request"
        for part in getattr(msg, "parts", []):
            kind = getattr(part, "part_kind", "") or type(part).__name__
            text = getattr(part, "content", None)
            if not isinstance(text, str):
                text = json.dumps(text, ensure_ascii=False, default=str) if text else ""
            label = {
                "user-prompt": "用户",
                "text": "模型",
                "thinking": "思考",
                "tool-call": "工具调用",
                "tool-return": "工具返回",
                "retry-prompt": "校验退回",
                "system-prompt": "系统",
            }.get(kind, kind)
            meta = ""
            if not is_req:
                bits = (getattr(msg, "model_name", ""), getattr(msg, "finish_reason", ""))
                meta = " · ".join(str(b) for b in bits if b)
            rows.append(
                SimpleNamespace(
                    role="req" if is_req else "resp",
                    label=label,
                    text=text,
                    meta=meta,
                )
            )
    return rows
