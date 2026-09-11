"""保存之前的三种"先看一眼"：schema 体检、模型能力报告、真跑一次。

**这个模块的路由必须先于 :mod:`agents` 注册**——``/agents/model-report`` 会被
``/agents/{slug}`` 通配吞掉，而症状是页面上出现一句"未知的 Agent：model-report"，
看起来像数据问题。装配顺序见 :mod:`xingcha.web.admin`。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import Response

from ... import contract as C
from ...api.runlog_mw import price
from ...core import builder
from ...core.builder import BuildOptions
from ...core.guarantee import resolve_tier
from ...core.schema_guard import SchemaRejected, validate_schema
from ...errors import XingchaError
from ...services import agent as agent_svc
from ...services import agent_test as test_svc
from ...services import run as run_svc
from .agent_view import (
    chain_rows,
    lint_ctx,
    model_report_ctx,
    prompting_from_form,
    test_history_row,
)
from .render import fmt_cost, render
from .security import (
    guard_mutation,
    require_admin,
    security_headers,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", include_in_schema=False)


@router.get("/agents/model-report")
async def agent_model_report(request: Request) -> Response:
    """选完模型就告诉你它能干什么，**不用等到第一次真调用**。

    **这条必须注册在 ``/agents/{slug}`` 之前。** FastAPI 按注册顺序匹配，字面路由
    被通配路由吞掉是静默的——请求落进 agent_edit，然后报一句"未知的 Agent：
    model-report"，而那句话指向一个根本不存在的问题。踩过一次。

    此前所有这类判定都只在调用那一刻生效：判档降级在保存后才提示、能力不支持要等
    真调用才报错、T2 的通道选错了同样如此。而这些信息在选完模型的那一刻就全都知道
    ——一半来自模型目录，一半来自 pydantic-ai 的 model profile。
    """
    await require_admin(request)
    q = request.query_params
    # 勾选状态由请求带回来（hx-include 把 cap_* 一起发上来）。不带的话，换一次模型
    # 就把已经勾上的清空了——而用户只是想看看这个模型行不行。
    checked = {k[len("cap_") :] for k in q if k.startswith("cap_") and q.get(k)}
    ctx = model_report_ctx(request, q.get("model", ""))
    offered = {n for n, _, _, _ in ctx["capabilities"]}
    return security_headers(
        render(
            request,
            "_model_report.html",
            {
                **ctx,
                "checked": checked,
                # 勾着、但这个模型用不了的，仍然当"已不再提供"渲染出来让人自己决定，
                # 不静默丢。
                "legacy_capabilities": sorted(checked - offered),
            },
        )
    )


@router.post("/agents/lint")
async def agent_lint(
    request: Request,
    output_schema: str = Form(default=""),
    tier: str = Form(default=""),
    csrf_token: str = Form(default=""),
) -> Response:
    """字段命名检查。HTMX 局部刷新，**不阻断保存**——这是建议不是规则。

    CSRF 两条路都接受：页面里 HTMX 走 hx-headers，而表单直接提交时走隐藏字段。
    只认其中一条会让另一条静默 403，而 403 在 HTMX 局部刷新里表现为"点了没反应"。
    """
    await guard_mutation(request, csrf_token)
    return security_headers(render(request, "_lint.html", lint_ctx(output_schema, tier)))


@router.post("/agents/test")
async def agent_test(request: Request, csrf_token: str = Form(default="")) -> Response:
    """按**当前表单**跑一次，不落库。

    testing 的对象是表单里此刻的内容，而不是已保存的版本——不然"改一句提示词看看
    效果"就得先保存，于是每试一次就多一个版本，而版本是不可删的。

    代价必须说在明处：这会真的调一次上游、真的花钱，而且**不走配额**（配额记的是
    调用方的用量，管理员在后台试跑不该记到某个业务的账上）。
    """
    await guard_mutation(request, csrf_token)
    state = request.app.state.xc

    raw = await request.form()
    probe = str(raw.get("test_input") or "").strip()
    slug = str(raw.get("slug") or "").strip()

    async def failed(message: str) -> Response:
        return await _trial_render(request, slug, {"ok": False, "message": message})

    if not probe:
        return await failed("先填一段测试输入——它就是调用方会发来的那条 user 消息。")
    if state.provider is None:
        return await failed("还没有配置上游 key。到「上游」页配好之后再试。")

    try:
        prompting = prompting_from_form(raw)
        inlined = (
            validate_schema(str(raw.get("output_schema") or ""))
            if str(raw.get("output_schema") or "").strip()
            else None
        )
        tier_raw = str(raw.get("tier") or "")
        model = str(raw.get("model") or "").strip()
        if not model:
            return await failed("先选一个模型。")

        choice = resolve_tier(
            C.Tier(tier_raw) if tier_raw else None,
            has_schema=inlined is not None,
            native_ok=builder.native_ok(
                model, state.provider, catalog_says=state.catalog.supports_native_schema(model)
            ),
        )
        settings_raw = {
            f: str(raw.get(f"ms_{f}") or "")
            for f, *_ in (
                *builder.model_settings_fields(),
                *builder.choice_settings_fields(),
            )
        }
        caps = builder.capabilities_from_form(raw)
        spec = builder.spec_from_form(
            name=str(raw.get("name") or "试运行"),
            description=None,
            instructions=str(raw.get("instructions") or ""),
            model=model,
            capabilities=caps or None,
            model_settings=builder.model_settings_from_form(settings_raw) or None,
            retries=max(0, min(5, int(str(raw.get("retries") or 2) or 2))),
            prompting=prompting,
        )
        if inlined is not None:
            spec["output_schema"] = inlined
        spec = builder.validate_spec(spec)
        rt = builder.build(
            spec_json=spec,
            tier=choice.tier,
            out_schema=inlined,
            provider=state.provider,
            options=BuildOptions(),
            concurrency=state.concurrency,
        )
    except (SchemaRejected, XingchaError, ValueError) as e:
        return await failed(str(getattr(e, "message", e)))

    return await _run_trial(
        request,
        slug=slug,
        probe=probe,
        rt=rt,
        tier=choice.tier.value,
        tier_note=choice.reason,
        model=model,
    )


async def _trial_render(request: Request, slug: str, ctx: dict[str, Any]) -> Response:
    """把这一次的结果连同最近几次一起渲染。

    历史与本次走同一个模板片段：两套渲染迟早在"这一列显示什么"上分叉，而这里
    要的恰恰是能把这次和上次并排比。
    """
    state = request.app.state.xc

    async with state.sessionmaker() as s:
        rows = await test_svc.recent(s, slug)
    history = [test_history_row(r) for r in rows]
    return security_headers(render(request, "_agent_test.html", {**ctx, "history": history}))


async def _run_trial(
    request: Request,
    *,
    slug: str,
    probe: str,
    rt: Any,
    tier: str,
    tier_note: str,
    model: str,
) -> Response:
    """执行一次试运行：调上游、记历史、渲染结果。

    表单试跑（新建/编辑页）与 Agent 卡片上的「测试」共用它——两处最容易分叉的地方
    正是"失败时显示什么"，而那恰好是最需要一致的部分。
    """
    state = request.app.state.xc

    conv = run_svc.apply_prompting(
        run_svc.to_conversation([{"role": "user", "content": probe}]), rt.prompting
    )

    started = time.monotonic()
    try:
        outcome = await run_svc.execute(rt, conv=conv, run_timeout=state.settings.run_timeout)
    except XingchaError as e:
        # 失败也把链路渲染出来：**看得见模型到底收到了什么**，才知道是提示词的问题
        # 还是 schema 的问题。只显示一句"失败了"等于什么都没说。失败同样入历史——
        # "上一版为什么挂"正是下一次要对照的东西。
        rows = chain_rows(getattr(e, "messages", []) or [])
        elapsed = time.monotonic() - started
        async with state.sessionmaker() as s:
            await test_svc.record(
                s,
                slug=slug,
                model=model,
                tier=tier,
                ok=False,
                prompt=probe,
                error=e.message,
                chain=[vars(r) for r in rows],
                elapsed_ms=int(elapsed * 1000),
            )
            await s.commit()
        return await _trial_render(
            request,
            slug,
            {"ok": False, "message": e.message, "rows": rows, "elapsed": f"{elapsed:.1f}"},
        )

    cost, source = price(
        state.catalog,
        outcome.model_id,
        {
            "input_tokens": outcome.input_tokens,
            "output_tokens": outcome.output_tokens,
            "cache_read_tokens": outcome.cache_read_tokens,
        },
    )
    rows = chain_rows(outcome.messages)
    elapsed = time.monotonic() - started
    async with state.sessionmaker() as s:
        await test_svc.record(
            s,
            slug=slug,
            model=model,
            tier=tier,
            ok=True,
            prompt=probe,
            output=outcome.content,
            chain=[vars(r) for r in rows],
            elapsed_ms=int(elapsed * 1000),
            input_tokens=outcome.input_tokens,
            output_tokens=outcome.output_tokens,
            requests=outcome.requests,
            violations=outcome.schema_violations,
            retries=outcome.schema_retries,
            cost_usd=str(cost) if cost is not None else None,
            cost_source=source,
        )
        await s.commit()
    return await _trial_render(
        request,
        slug,
        {
            "ok": True,
            "rows": rows,
            "output": outcome.content,
            "tier": tier,
            "tier_note": tier_note,
            "elapsed": f"{elapsed:.1f}",
            "input_tokens": outcome.input_tokens,
            "output_tokens": outcome.output_tokens,
            "requests": outcome.requests,
            "retries": outcome.schema_retries,
            "violations": outcome.schema_violations,
            "cost": fmt_cost(str(cost)) if cost is not None else "—",
            "cost_source": source,
        },
    )


@router.post("/agents/{slug}/trial")
async def agent_trial(request: Request, slug: str, csrf_token: str = Form(default="")) -> Response:
    """跑一次**已保存的当前版本**，不改动它。

    与表单试跑的区别只有一个：跑的是库里那一版，而不是页面上没保存的内容。
    Agent 列表页上的「测试」用它——那里没有表单可以取值。
    """
    await guard_mutation(request, csrf_token)
    state = request.app.state.xc

    raw = await request.form()
    probe = str(raw.get("test_input") or "").strip()
    if not probe:
        return await _trial_render(request, slug, {"ok": False, "message": "先填一段测试输入。"})
    if state.provider is None:
        return await _trial_render(
            request,
            slug,
            {"ok": False, "message": "还没有配置上游 key。到「上游」页配好之后再试。"},
        )

    try:
        async with state.sessionmaker() as s:
            a = await agent_svc.resolve(s, slug, include_inactive=True)
        spec = json.loads(a.spec_json)
        rt = builder.build(
            spec_json=spec,
            tier=C.Tier(a.tier),
            out_schema=json.loads(a.out_schema) if a.out_schema else None,
            provider=state.provider,
            options=BuildOptions(),
            concurrency=state.concurrency,
        )
    except (XingchaError, ValueError) as e:
        return await _trial_render(
            request, slug, {"ok": False, "message": str(getattr(e, "message", e))}
        )

    return await _run_trial(
        request,
        slug=slug,
        probe=probe,
        rt=rt,
        tier=a.tier,
        tier_note=f"已保存的 v{a.version}",
        model=str(spec.get("model", "")),
    )
