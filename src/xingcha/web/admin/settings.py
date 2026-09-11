"""设置页：后台密码与调用追踪（OTLP）目标。"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from ... import contract as C
from ...core.urlguard import UnsafeUpstreamURL, check_upstream_url
from ...db import migrate
from ...services import setting as setting_svc
from ...services import trace_targets
from ...services import websession as ws
from .render import page
from .security import (
    Denied,
    guard_mutation,
    require_admin,
    security_headers,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", include_in_schema=False)


async def _settings_ctx(
    request: Request,
    *,
    password_error: str | None = None,
    trace_error: str | None = None,
    trace_form: Any = None,
) -> dict[str, Any]:
    """设置页的模板上下文。

    抽出来是为了让 POST 失败时能**就地回显**：密码填错的时候直接渲染这一页，
    保留已经填好的地址与 public key，而不是跳到一个独立的错误页。

    此前是后者，代价很具体：改可观测配置要填地址 + 两把 key + 密码，密码错一次
    就全部重填一遍。而"密码错"恰恰是这个表单最常见的失败。
    """
    state = request.app.state.xc

    async with state.sessionmaker() as s:
        raw_key = await setting_svc.get(s, state.keyring, C.SETTING_KEY_OPENROUTER_API_KEY)
        base_url = await setting_svc.get(s, state.keyring, C.SETTING_KEY_OPENROUTER_BASE_URL)
        targets = await trace_targets.list_all(s, state.keyring)
        active = await trace_targets.active_name(s, state.keyring)
        has_db_password = await ws.has_password(s)

    return {
        "masked_key": setting_svc.mask(raw_key) if raw_key else "",
        "base_url": base_url or C.OPENROUTER_DEFAULT_BASE_URL,
        "catalog_count": len(state.catalog.all()),
        "catalog_stale": state.catalog.is_stale,
        "data_dir": str(state.settings.data_dir.resolve()),
        "db_revision": migrate.current_revision(state.settings.db_path) or "—",
        # 表单是**新增用的**，所以默认全空，只在提交失败时回填这一次填的内容。
        #
        # 曾经把已保存的地址与 public key 灌回表单，于是它同时是"新增"和"编辑"，
        # 而页面上分不出你正在改哪一条——想加第二条得先手动清空。列表在左边，
        # 表单就该只管加。
        "trace_form": trace_form,
        "trace_targets": [
            SimpleNamespace(
                name=t.name,
                endpoint=t.endpoint,
                public_key=t.public_key,
                has_secret=bool(t.secret_key),
                active=t.name.lower() == (active or "").lower(),
            )
            for t in targets
        ],
        "trace_service_name": state.settings.trace_service_name,
        "trace_on": state.tracing is not None,
        "trace_include_content": state.settings.trace_include_content,
        "password_error": password_error,
        "trace_error": trace_error,
        "env_managed": ws.env_password_in_effect(
            "x" if has_db_password else None, state.settings.admin_password
        ),
        # 库里已有密码而环境变量也设了：那一项被忽略，必须说出来，
        # 否则用户改了 .env、重启、发现没变化，而根因看不见。
        "admin_password_env_ignored": bool(has_db_password and state.settings.admin_password),
    }


async def _render_settings(request: Request, **errors: Any) -> Response:
    return await page(request, "settings.html", await _settings_ctx(request, **errors))


@router.get("/settings")
async def settings_page(request: Request) -> Response:
    await require_admin(request)
    return await _render_settings(request)


@router.post("/settings/password")
async def change_password(
    request: Request,
    current: str = Form(...),
    new_password: str = Form(...),
    confirm: str = Form(...),
    csrf_token: str = Form(default=""),
) -> Response:
    """改后台密码。

    改完**吊销所有会话，包括当前这个**——用户会被踢回登录页。这不是疏漏：
    改密码的场景往往正是"我怀疑密码泄漏了"，那一刻留着任何一个旧会话都等于没改。
    自己也被踢是可接受的代价（重新登录一次），而"只踢别人"需要区分会话来源，
    多一层不必要的逻辑。

    忘了密码走 ``xingcha admin reset-password``——密码只存 argon2id 哈希，
    这里也取不回来。
    """
    await guard_mutation(request, csrf_token)
    state = request.app.state.xc

    # 校验失败一律**就地回显**，不跳独立错误页。这个表单三个字段都是密码，
    # 跳走之后要全部重填，而"两次不一致"和"当前密码错"都是高频失误。
    if new_password != confirm:
        return await _render_settings(
            request, password_error="两次输入的新密码不一致，未做任何修改。"
        )
    if len(new_password) < C.MIN_ADMIN_PASSWORD_LEN:
        return await _render_settings(
            request, password_error=f"新密码至少 {C.MIN_ADMIN_PASSWORD_LEN} 位，未做任何修改。"
        )

    async with state.sessionmaker() as s:
        admin = await ws.get_admin(s)
        if admin is None:
            raise Denied("数据库里没有管理员账号。")
        if ws.env_password_in_effect(admin.password_hash, state.settings.admin_password):
            # 改了也不生效（登录按环境变量校验），所以直接拒绝。
            # 假装成功是最坏的选择：用户以为换了密码，而旧的那个仍然能登。
            return await _render_settings(
                request,
                password_error=(
                    "密码由环境变量 XINGCHA_ADMIN_PASSWORD 托管，在这里改不生效。"
                    "请改 .env 里的那一项并重启服务。"
                ),
            )
        if not ws.verify_admin_password(
            admin.password_hash, current, state.settings.admin_password
        ):
            return await _render_settings(request, password_error="当前密码不正确，未做任何修改。")
        if new_password == current:
            return await _render_settings(
                request, password_error="新密码与当前密码相同，未做任何修改。"
            )
        admin.password_hash = ws.hash_password(new_password)
        await ws.revoke_all(s)
        await s.commit()

    resp = security_headers(RedirectResponse("/admin/login", status_code=303))
    # 会话已经在库里被吊销，cookie 留着只会让下一次请求白跑一遍鉴权
    resp.delete_cookie("xc_session", path="/admin")
    return resp


@router.post("/settings/trace")
async def save_trace_target(
    request: Request,
    password: str = Form(...),
    name: str = Form(default=""),
    endpoint: str = Form(default=""),
    public_key: str = Form(default=""),
    secret_key: str = Form(default=""),
    csrf_token: str = Form(default=""),
) -> Response:
    """新增或按名字覆盖一个上报目标。

    要密码，理由和上游 key 一样：**上报打开意味着提示词与模型输出会被送到一个
    外部地址**。这个表单一旦被跨站提交，攻击者就得到了一份持续到达的对话副本——
    后果与偷走 key 是同一个量级。

    校验**全部在写库之前**：密码不对或地址被拒时一个字节都不改，并且就地回显这一页
    （保留已填内容），而不是跳到独立的错误页——那样地址与两把 key 要全部重填一遍，
    而"密码错"正是这个表单最常见的失败。

    endpoint 过 SSRF 守卫：它是一个"服务端会主动去打"的地址，和上游地址同类。
    """
    await guard_mutation(request, csrf_token)
    state = request.app.state.xc
    # 回显用：secret key 不在其中——回显它就是把它写进 HTML。
    form = SimpleNamespace(
        name=name.strip(), endpoint=endpoint.strip(), public_key=public_key.strip()
    )

    if not form.name:
        return await _render_settings(request, trace_error="给这个目标起个名字。", trace_form=form)
    if not form.endpoint:
        return await _render_settings(request, trace_error="上报地址不能为空。", trace_form=form)

    async with state.sessionmaker() as s:
        admin = await ws.get_admin(s)
        # 环境变量托管时库里没有哈希，verify_password 会对任何输入返回 False。
        if admin is None or not ws.verify_admin_password(
            admin.password_hash, password, state.settings.admin_password
        ):
            return await _render_settings(
                request, trace_error="当前密码不正确，未做任何修改。", trace_form=form
            )
        try:
            # allow_private：自建 Langfuse 基本就在内网（同一个 docker network
            # 或者 10.x）。一律拒私有网段会把最主流的自建部署挡死，而挡死之后
            # 人们会去用托管服务——那正好是更差的隐私结果。
            # 链路本地（云元数据端点）仍然拒。
            checked = check_upstream_url(form.endpoint, allow_private=True)
        except UnsafeUpstreamURL as e:
            return await _render_settings(
                request, trace_error=f"上报地址被拒绝：{e}", trace_form=form
            )

        first = not await trace_targets.list_all(s, state.keyring)
        await trace_targets.upsert(
            s,
            state.keyring,
            name=form.name,
            endpoint=checked.url,
            public_key=form.public_key,
            secret_key=secret_key,
        )
        # 第一条自动启用：存完还停着的话，这个动作在页面上看不出任何效果。
        # 之后再加的**不**自动启用——那会把正在用的那条静默切走。
        if first:
            await trace_targets.set_active(s, state.keyring, form.name)
        await s.commit()

    await _reload_tracing(state)
    return security_headers(RedirectResponse("/admin/settings", status_code=303))


async def _reload_tracing(state: Any) -> None:
    """按库里的最新配置重装追踪管道。

    旧管道要**先关掉**再换新的，否则上一个 BatchSpanProcessor 的后台线程会一直
    留着。运行时缓存也要清：Agent 的埋点绑在构造时的 model 上。
    """
    from ...app import load_tracing

    if state.tracing is not None:
        state.tracing.shutdown()
        state.tracing = None
    await load_tracing(state)
    state.runtimes.clear()


@router.post("/settings/trace/activate")
async def activate_trace(
    request: Request, name: str = Form(default=""), csrf_token: str = Form(default="")
) -> Response:
    """启用某一条，或（``name`` 为空时）全部停用。

    **这一个不要密码，改地址那个要。** 差别不在于哪个听起来更危险，而在于攻击面：
    改地址是"把对话副本送到我指定的地方"，启用只能送到**管理员自己早就选定并存下
    来的**那个地址。真正的门是选目的地那一步，那里守着密码；这里有 CSRF 就够了
    ——而给一个每天要用的开关加密码，结果是没人去关它。
    """
    await guard_mutation(request, csrf_token)
    state = request.app.state.xc

    async with state.sessionmaker() as s:
        target = name.strip()
        if target and await trace_targets.get(s, state.keyring, target) is None:
            return await _render_settings(request, trace_error=f"没有名为「{target}」的目标。")
        await trace_targets.set_active(s, state.keyring, target or None)
        await s.commit()

    await _reload_tracing(state)
    return security_headers(RedirectResponse("/admin/settings", status_code=303))


@router.post("/settings/trace/delete")
async def delete_trace_target(
    request: Request,
    name: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(default=""),
) -> Response:
    """删掉一条，连同它的两把 key。

    与"停用"分开：停用可逆，这个不可逆。要密码——它销毁的是加密存着的凭据，
    而这个后台里凡是能销毁东西的动作都过同一道门。
    """
    await guard_mutation(request, csrf_token)
    state = request.app.state.xc

    async with state.sessionmaker() as s:
        admin = await ws.get_admin(s)
        if admin is None or not ws.verify_admin_password(
            admin.password_hash, password, state.settings.admin_password
        ):
            return await _render_settings(request, trace_error="当前密码不正确，未做任何修改。")
        await trace_targets.remove(s, state.keyring, name)
        await s.commit()

    await _reload_tracing(state)
    return security_headers(RedirectResponse("/admin/settings", status_code=303))
