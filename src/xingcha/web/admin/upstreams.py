"""上游页：供应商增删、连通性体检与切换。

「加一个供应商」与「切到哪个供应商」是同一页上的两件事，所以放同一个模块——
此前它们分居 routes.py 的两处，改一处忘另一处发生过。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from ... import contract as C
from ...core.upstream import UpstreamConfig
from ...core.urlguard import UnsafeUpstreamURL, check_upstream_url
from ...foundation.errors import redact
from ...services import agent as agent_svc
from ...services import providers as provider_svc
from ...services import setting as setting_svc
from ...services import upstream_env as ue
from ...services import websession as ws
from .render import page, render
from .security import (
    Denied,
    guard_mutation,
    require_admin,
    security_headers,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", include_in_schema=False)


def _form_error(request: Request, message: str) -> Response:
    """表单里的一条错误，**只换结果区那一小块**。

    整页重渲染会把用户填的东西清空——而这个表单要填名字、地址、key、密码四样，
    最常错的是地址（少了或多了 ``/v1``）。清空一次就等于罚他重输四遍。
    """
    return security_headers(
        render(request, "_upstream_check.html", {"ok": False, "message": message})
    )


@router.post("/upstreams/providers")
async def add_provider(
    request: Request,
    name: str = Form(...),
    base_url: str = Form(...),
    api_key: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(default=""),
) -> Response:
    """添加一个供应商到列表。**只保存，不切换。**

    加和用是两件事：加进来是"我以后可能用它"，切过去是"现在就换出口"，而后者会打断
    所有现有 Agent。把两件事绑在一个按钮上，等于每次新增供应商都强制来一次出口变更。
    要用它就在左边列表点「检查并切换」——那条路径会先列出哪些 Agent 会失效。

    **顺序是：全部校验通过（含真连一次上游）之后，才写。**

    此前是"先存下来再探测"，代价是失败的条目留在列表里，用户看到一个从来没通过的
    条目还得自己去删。而"不丢输入"根本不该靠落库实现——这个表单走 htmx，失败时页面
    不重载，输入本来就还在。

    要密码：这个表单一旦被跨站提交，付费 key 就会被送到攻击者的服务器。CSRF 三层
    之外再加一道，因为这是全后台后果最严重的一个操作。
    """
    await guard_mutation(request, csrf_token)

    state = request.app.state.xc

    # --- 只读校验，一个字节都不写 ---
    async with state.sessionmaker() as s:
        admin = await ws.get_admin(s)
    # **必须走 verify_admin_password，不能用 verify_password。** 后者只查库里的
    # argon2id 哈希；而密码由环境变量托管时库里根本没有哈希，于是它对任何输入都
    # 返回 False —— 用户输的是对的，却永远被告知"密码不正确"。实际撞过这个。
    if admin is None or not ws.verify_admin_password(
        admin.password_hash, password, state.settings.admin_password
    ):
        return _form_error(request, "当前密码不正确，什么都没保存。")

    cleaned_name = name.strip()
    if not cleaned_name or len(cleaned_name) > C.PROVIDER_NAME_MAX:
        return _form_error(request, f"名字必须是 1–{C.PROVIDER_NAME_MAX} 个字符。")
    try:
        checked = check_upstream_url(base_url.strip())
    except UnsafeUpstreamURL as e:
        return _form_error(request, f"地址被拒绝：{e}")
    if not api_key.strip():
        return _form_error(request, "API key 不能为空。")

    # --- 真连一次上游。**拉不通就到此为止，不保存、不切换。** ---
    probe = await ue.probe_switch(
        ref=cleaned_name,
        source="saved",
        base_url=checked.url,
        api_key=api_key.strip(),
        agent_models={},
        timeout=state.settings.request_timeout,
    )
    if not probe.can_switch:
        return _form_error(
            request,
            f"没有保存：拉 {checked.url}/models 失败（{probe.error or '目录为空'}）。"
            "地址通常是少了或多了 /v1。改完再点一次。",
        )

    # --- 到这里才写。**只进列表，不动当前出口。** ---
    async with state.sessionmaker() as s:
        await provider_svc.upsert(
            s,
            state.keyring,
            name=cleaned_name,
            base_url=checked.url,
            api_key=api_key.strip(),
        )
        await s.commit()

    # htmx 提交的表单：用 HX-Redirect 让浏览器整页跳转。
    # 直接回 303 的话 htmx 会去跟随、把整页 HTML 塞进那个结果小方块里。
    resp = Response(status_code=204)
    resp.headers["HX-Redirect"] = "/admin/upstreams"
    return security_headers(resp)


@router.post("/upstreams/check")
async def check_provider(
    request: Request,
    base_url: str = Form(...),
    api_key: str = Form(default=""),
    name: str = Form(default=""),
    csrf_token: str = Form(default=""),
) -> Response:
    """只检查，**什么都不写**。给「添加供应商」表单在保存之前用。

    存在的理由：这个表单要填名字、地址、key、密码四样，而最常错的是地址
    （少了或多了 ``/v1``）。没有干跑的时候，唯一的验证方式是"保存一次看看"——
    而那要么写坏配置，要么把四个输入全丢掉重填。

    返回 HTML 片段（htmx 换进表单下方），所以**不碰用户已填的任何输入**。
    """
    await guard_mutation(request, csrf_token)

    state = request.app.state.xc

    try:
        checked = check_upstream_url(base_url.strip())
    except UnsafeUpstreamURL as e:
        return security_headers(
            render(request, "_upstream_check.html", {"ok": False, "message": f"地址被拒绝：{e}"})
        )
    if not api_key.strip():
        return security_headers(
            render(request, "_upstream_check.html", {"ok": False, "message": "API key 不能为空。"})
        )

    probe = await ue.probe_switch(
        ref=name.strip() or "待添加",
        source="saved",
        base_url=checked.url,
        api_key=api_key.strip(),
        agent_models={},
        timeout=state.settings.request_timeout,
    )
    return security_headers(
        render(
            request,
            "_upstream_check.html",
            {
                "ok": probe.can_switch,
                "message": (
                    f"通了：{checked.url} 上拉到 {probe.model_count} 个模型。"
                    if probe.can_switch
                    else f"拉 {checked.url}/models 失败（{probe.error or '目录为空'}）。"
                    "地址通常是少了或多了 /v1。"
                ),
            },
        )
    )


@router.post("/upstreams/providers/delete")
async def delete_provider(
    request: Request,
    name: str = Form(...),
    csrf_token: str = Form(default=""),
) -> Response:
    """删掉一个已保存的供应商。

    **不动当前出口**：删的是"列表里那一行"，而不是"正在用的配置"。两者混在一起的
    话，删一行会让服务当场失去上游，而用户以为自己只是在整理列表。
    """
    await guard_mutation(request, csrf_token)

    state = request.app.state.xc
    async with state.sessionmaker() as s:
        await provider_svc.remove(s, state.keyring, name)
        await s.commit()
    return security_headers(RedirectResponse("/admin/upstreams", status_code=303))


@router.post("/settings/test")
async def test_upstream(request: Request) -> Response:
    """用**已保存**的配置做一次自检。

    刻意不接受表单里的地址：那样这个按钮就成了一个"带着真实 key 去打任意 URL"的
    SSRF 原语。要测新地址，先保存（保存时会过 urlguard）。
    """
    await guard_mutation(request, None)
    state = request.app.state.xc

    cfg: UpstreamConfig | None = state.upstream.config
    if cfg is None:
        return security_headers(
            render(request, "_test_result.html", {"ok": False, "message": "还没有配置上游 key。"})
        )

    ok = await state.catalog.refresh(state.upstream.client(), cfg.api_key)
    if not ok:
        # 错误文本可能带完整 URL，脱敏后再回显

        return security_headers(
            render(
                request,
                "_test_result.html",
                {"ok": False, "message": redact(state.catalog.last_error or "未知错误")},
            )
        )

    models = state.catalog.all()
    return security_headers(
        render(
            request,
            "_test_result.html",
            {
                "ok": True,
                "count": len(models),
                "native": sum(1 for m in models if m.supports_native_schema),
            },
        )
    )


async def upstream_context(request: Request, *, error: str | None = None) -> dict[str, Any]:

    state = request.app.state.xc
    async with state.sessionmaker() as s:
        active_ref = await setting_svc.get(s, state.keyring, C.SETTING_KEY_UPSTREAM_ACTIVE_ENV)
        raw_key = await setting_svc.get(s, state.keyring, C.SETTING_KEY_OPENROUTER_API_KEY)
        base_url = await setting_svc.get(s, state.keyring, C.SETTING_KEY_OPENROUTER_BASE_URL)
        agents = await agent_svc.list_all(s)
        saved = await provider_svc.list_all(s, state.keyring)

    default_key, default_base = ue.default_pair(state.settings)

    # 切换列表 = .env 里的默认那一对 + 扫到的厂商 key + 用户自己加的。
    #
    # **默认那一对必须在列表里**，否则切到别家之后回不来——它没有"厂商变量名"，
    # 此前也就没有对应的一行，只能靠重新写 .env + 重启。实际撞过。
    #
    # 取值走 default_pair 而不是 default_from_env：uv 直跑那条路上 .env 只被 pydantic
    # 读进 Settings，进程环境里没有它，于是这一行**只在 docker 下出得来**。同样实际撞过。
    options: list[dict[str, Any]] = []
    if default_key:
        options.append(
            {
                "source": "env",
                "ref": C.ENV_DEFAULT_API_KEY,
                "label": ".env 里的默认",
                "detail": C.ENV_DEFAULT_API_KEY,
                "base_url": default_base or "",
                "masked": ue.mask(default_key),
                "has_catalog": True,
                "removable": False,
            }
        )
    options += [
        {
            "source": "env",
            "ref": u.env_name,
            "label": u.label,
            "detail": u.env_name,
            "base_url": u.base_url or "",
            "masked": u.masked,
            "has_catalog": u.has_catalog,
            "removable": False,
        }
        for u in ue.discover()
    ]
    options += [
        {
            "source": "saved",
            "ref": p.name,
            "label": p.name,
            "detail": "用户添加",
            "base_url": p.base_url,
            "masked": p.masked,
            "has_catalog": True,
            "removable": True,
        }
        for p in saved
    ]

    active_label = "手动填写"
    if active_ref:
        match = next((o for o in options if o["ref"].lower() == active_ref.lower()), None)
        active_label = match["label"] if match else C.vendor_label(active_ref)
    elif default_key:
        active_label = ".env 里的默认"

    return {
        "active": {
            "ref": active_ref or "",
            "label": active_label,
            "masked": setting_svc.mask(raw_key) if raw_key else "",
            "base_url": base_url or "",
            "configured": bool(raw_key),
            "model_count": len(state.catalog.all()),
            "catalog_stale": state.catalog.is_stale,
        },
        "options": options,
        "agent_count": len(agents),
        "error": error,
        "provider_name_max": C.PROVIDER_NAME_MAX,
        # 容器里扫不到宿主环境变量（Docker 不继承）。页面必须说，否则本机看到一排、
        # 上线发现空的会被当成功能坏了。
        "in_container": Path("/.dockerenv").exists(),
    }


async def render_upstreams(request: Request, *, error: str | None = None) -> Response:
    return await page(request, "upstreams.html", await upstream_context(request, error=error))


@router.get("/upstreams")
async def upstreams_page(request: Request) -> Response:
    await require_admin(request)
    return await render_upstreams(request)


async def _resolve_candidate(request: Request, source: str, ref: str) -> tuple[str, str]:
    """把（来源，标识）解析成 ``(api_key, 默认 base_url)``。

    两种来源必须走同一个出口，否则"探测用环境里的、切换用库里的"这类错配只会在
    某一条路径上炸——而两条路径的代码看起来一模一样。
    """

    if source == "saved":
        state = request.app.state.xc
        async with state.sessionmaker() as s:
            got = await provider_svc.get(s, state.keyring, ref)
        if got is None:
            raise Denied(f"没有名为 {ref} 的供应商——是不是已经删掉了？")
        return got.api_key, got.base_url

    if source != "env":
        raise Denied(f"未知的来源：{source}")

    # 默认那一对要和列表用同一个出口（default_pair）：列表能看见、点下去却说"读不到值"
    # 是最糟的一种不一致——uv 直跑那条路上进程环境里本来就没有它。
    if ref.upper() in {a.upper() for a in C.ENV_API_KEY_ALIASES}:
        state = request.app.state.xc
        api_key, default_base = ue.default_pair(state.settings)
        if not api_key:
            raise Denied(f"{ref} 现在读不到值——是不是已经从 .env 里删了？（改完要重启）")
        return api_key, default_base or ""

    api_key = ue.read_key(ref)
    if not api_key:
        raise Denied(f"现在读不到 {ref} 的值——是不是已经从部署配置里删掉了？（改完要重启）")
    return api_key, C.base_url_for_env(ref.upper()) or ""


@router.post("/upstreams/probe")
async def upstreams_probe(
    request: Request,
    ref: str = Form(...),
    source: str = Form(default="env"),
    base_url: str = Form(default=""),
    csrf_token: str = Form(default=""),
) -> Response:
    """切换前的体检。**只读**——不写任何设置，不动运行时。

    分成 probe / switch 两步而不是一步切完：切上游会打断所有现有 Agent（模型名在
    新上游不存在），而那个后果必须在切之前看得见。
    """
    await guard_mutation(request, csrf_token)

    state = request.app.state.xc
    api_key, known_base = await _resolve_candidate(request, source, ref)
    resolved = (base_url.strip() or known_base).strip()
    if not resolved:
        raise Denied(f"{ref} 不是已知厂商，请填写 base_url。")
    try:
        checked = check_upstream_url(resolved)
    except UnsafeUpstreamURL as e:
        raise Denied(f"base_url 被拒绝：{e}") from e

    async with state.sessionmaker() as s:
        agents = await agent_svc.list_all(s)
    # 模型名在 spec_json 里（AgentSpec 的一个字段），没有独立列
    models: dict[str, str] = {}
    for a, v in agents:
        if v is None:
            continue
        model = json.loads(v.spec_json).get("model")
        if isinstance(model, str) and model:
            models[a.slug] = model

    probe = await ue.probe_switch(
        ref=ref,
        source=source,
        base_url=checked.url,
        api_key=api_key,
        agent_models=models,
        timeout=state.settings.request_timeout,
    )
    return security_headers(
        render(request, "_upstream_probe.html", {"probe": probe, "csrf": csrf_token})
    )


@router.post("/upstreams/switch")
async def upstreams_switch(
    request: Request,
    ref: str = Form(...),
    base_url: str = Form(...),
    source: str = Form(default="env"),
    csrf_token: str = Form(default=""),
) -> Response:
    """真正切过去。

    选中的 key 从环境变量读出来后**加密落库**——环境只是发现来源，不是长期存放处
    （它会进 ``docker inspect`` 与 ``/proc/<pid>/environ``）。

    切完必须做三件收尾，少一件就会留下难查的问题：
    1. 重装上游客户端（``load_upstream``）；
    2. **重拉模型目录**——它是判档与定价的主价源，不拉的话每条记录都是
       ``cost_source=unknown``；
    3. **清运行时缓存**——Agent 实例把 provider 烤进去了，不清则旧 key 继续被用。
    """
    await guard_mutation(request, csrf_token)

    state = request.app.state.xc
    api_key, _ = await _resolve_candidate(request, source, ref)
    try:
        checked = check_upstream_url(base_url.strip())
    except UnsafeUpstreamURL as e:
        raise Denied(f"base_url 被拒绝：{e}") from e

    # **自己再探一次，不信上一步。**
    #
    # 这个 POST 是一个普通表单，可以被后退、刷新、重放；而"上一步已经检查过了"
    # 是一个关于用户浏览器行为的假设。写坏配置的代价是服务当场不可用，
    # 而页面上一切正常——只是模型目录空了。多一次几百毫秒的请求换掉这个假设。
    probe = await ue.probe_switch(
        ref=ref,
        source=source,
        base_url=checked.url,
        api_key=api_key,
        agent_models={},
        timeout=state.settings.request_timeout,
    )
    if not probe.can_switch:
        return await render_upstreams(
            request,
            error=(
                f"没有切到「{probe.label}」：用这把 key 拉 {checked.url}/models 失败"
                f"（{probe.error or '目录为空'}）。当前出口未改动。"
            ),
        )

    async with state.sessionmaker() as s:
        await setting_svc.set_(s, state.keyring, C.SETTING_KEY_OPENROUTER_API_KEY, api_key)
        await setting_svc.set_(s, state.keyring, C.SETTING_KEY_OPENROUTER_BASE_URL, checked.url)
        await setting_svc.set_(s, state.keyring, C.SETTING_KEY_UPSTREAM_ACTIVE_ENV, ref)
        await s.commit()

    from ...app import load_upstream

    await load_upstream(state)
    up = state.upstream.config
    if up is not None:
        await state.catalog.refresh(state.upstream.client(), up.api_key)
    state.runtimes.clear()

    return security_headers(RedirectResponse("/admin/upstreams", status_code=303))
