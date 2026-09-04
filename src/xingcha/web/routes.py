"""管理后台。

安全约束集中在 :func:`guard_mutation`——**每一个改状态的请求都必须经过它**。
后台暴露在公网上，而它里面有一个能改写上游 base_url 的表单：一次成功的 CSRF
就等于把付费 key 送到攻击者的服务器。所以三层叠加：SameSite=Strict cookie、
double-submit token、Origin/Sec-Fetch-Site 校验。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from .. import __version__
from .. import contract as C
from ..core.upstream import UpstreamConfig
from ..core.urlguard import UnsafeUpstreamURL, check_upstream_url
from ..db.models import Run, RunUsage
from ..services import auth as auth_svc
from ..services import setting as setting_svc
from ..services import websession as ws

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
router = APIRouter(prefix="/admin", include_in_schema=False)

_throttle = ws.LoginThrottle()


# =============================================================================
# 安全
# =============================================================================


class Denied(Exception):
    """后台层面的拒绝。不走 /v1 的错误契约——那是给 SDK 用的，这里是给人看的。"""

    def __init__(self, message: str, status: int = 403) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def security_headers(resp: Response) -> Response:
    """每个后台响应都带上。

    ``frame-ancestors 'none'`` 挡点击劫持——否则攻击者可以把后台套进一个透明 iframe，
    诱导管理员"点一下"，绕到与 CSRF 相同的结果。
    """
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["X-Frame-Options"] = "DENY"
    return resp


def check_origin(request: Request) -> None:
    """校验请求确实来自本站。

    ``Sec-Fetch-Site`` 是现代浏览器一定会带的，且不可被脚本伪造；``Origin`` 作为
    老浏览器的回退。两个都没有时放行——非浏览器客户端（curl）本来就不受 CSRF 影响，
    而卡住它们只会让排障变难。
    """
    site = request.headers.get("sec-fetch-site")
    if site is not None:
        if site not in {"same-origin", "same-site", "none"}:
            raise Denied(f"跨站请求被拒绝（Sec-Fetch-Site: {site}）")
        return

    origin = request.headers.get("origin")
    if origin:
        host = request.headers.get("host", "")
        if not (origin.endswith(f"//{host}") or origin.endswith(f".{host}")):
            raise Denied("跨站请求被拒绝（Origin 与 Host 不符）")


async def current_session(request: Request):
    state = request.app.state.xc
    token = request.cookies.get(ws.SESSION_COOKIE)
    async with state.sessionmaker() as s:
        row = await ws.resolve(s, token)
        await s.commit()
        return row


async def require_admin(request: Request):
    row = await current_session(request)
    if row is None:
        raise Denied("未登录", status=401)
    return row


async def guard_mutation(request: Request, csrf_token: str | None) -> None:
    """**每一个改状态的请求都要过这里。**

    三层叠加不是冗余：SameSite 挡不住老浏览器；double-submit 挡不住能读到页面的
    同站脚本注入；Origin 校验挡不住不发这些头的客户端。三层一起才覆盖得住。
    """
    check_origin(request)
    row = await require_admin(request)
    header_token = request.headers.get(ws.CSRF_HEADER)
    if not (ws.csrf_matches(row, csrf_token) or ws.csrf_matches(row, header_token)):
        raise Denied("CSRF 校验失败。请刷新页面后重试。")


# =============================================================================
# 渲染
# =============================================================================


async def _csrf_for(request: Request) -> str:
    """页面里用的 CSRF 值。

    存的是哈希，所以明文只能来自 cookie 之外的一次性传递——这里用一个独立的、
    与会话绑定的 cookie 承载，攻击者的页面读不到它（同源策略）。
    """
    return request.cookies.get("xc_csrf", "")


def _render(request: Request, template: str, ctx: dict[str, Any]) -> HTMLResponse:
    state = request.app.state.xc
    base = {
        "version": __version__,
        "contract": C.CONTRACT_VERSION,
        "current": request.url.path.rstrip("/") or "/admin",
        "public_url": state.settings.public_url
        or f"http://{state.settings.host}:{state.settings.port}",
        "flash": None,
        "theme": "",
    }
    resp = templates.TemplateResponse(request, template, {**base, **ctx})
    return security_headers(resp)  # type: ignore[return-value]


def _fmt_time(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso).strftime("%m-%d %H:%M")
    except ValueError:
        return iso[:16]


def _fmt_cost(value: str | None) -> str:
    """费用展示。

    ``None`` 是"无法定价"，与真实的 0 费用不同——所以显示 ``—`` 而不是 ``0``。
    """
    if value is None:
        return "—"
    try:
        d = Decimal(value)
    except Exception:
        return "—"
    if d == 0:
        return "0"
    return f"{d:.6f}".rstrip("0").rstrip(".")


# =============================================================================
# 登录
# =============================================================================


@router.get("/login")
async def login_page(request: Request) -> Response:
    state = request.app.state.xc
    async with state.sessionmaker() as s:
        setup = not await ws.has_password(s)
    if await current_session(request) is not None:
        return RedirectResponse("/admin", status_code=303)

    csrf = await _ensure_csrf_cookie(request)
    resp = _render(
        request,
        "login.html",
        {"setup": setup, "action": "/admin/login", "error": None, "csrf": csrf.value},
    )
    csrf.apply(resp)
    return resp


@dataclass
class _Csrf:
    value: str
    fresh: bool

    def apply(self, resp: Response) -> None:
        if self.fresh:
            resp.set_cookie(
                "xc_csrf",
                self.value,
                httponly=False,  # 表单要读它；它本身不是凭证，只是"你能读到本站页面"的证明
                samesite="strict",
                secure=True,
                path="/admin",
            )


async def _ensure_csrf_cookie(request: Request) -> _Csrf:
    existing = request.cookies.get("xc_csrf")
    if existing:
        return _Csrf(existing, fresh=False)
    import secrets

    return _Csrf(secrets.token_urlsafe(32), fresh=True)


@router.post("/login")
async def login(
    request: Request,
    password: str = Form(...),
    confirm: str = Form(default=""),
    csrf_token: str = Form(default=""),
) -> Response:
    check_origin(request)
    state = request.app.state.xc
    throttle_key = request.client.host if request.client else "unknown"

    try:
        _throttle.check(throttle_key)
    except ws.LoginRateLimited as e:
        return _login_error(request, str(e))

    async with state.sessionmaker() as s:
        admin = await ws.get_admin(s)
        if admin is None:
            return _login_error(request, "数据库里没有管理员账号，请检查安装。")

        setup = not admin.password_hash
        if setup:
            if len(password) < 12:
                return _login_error(request, "密码至少 12 位。", setup=True)
            if password != confirm:
                return _login_error(request, "两次输入的密码不一致。", setup=True)
            admin.password_hash = ws.hash_password(password)
            log.info("已设置管理员密码")
        else:
            if not ws.verify_password(admin.password_hash, password):
                _throttle.record_failure(throttle_key)
                # 不区分"用户不存在"与"密码错误"，也不提示剩余次数
                return _login_error(request, "密码不正确。")
            if admin.password_hash and ws.needs_rehash(admin.password_hash):
                admin.password_hash = ws.hash_password(password)

        new = await ws.create(s, admin.id, ttl_hours=state.settings.session_ttl_hours)
        await s.commit()

    _throttle.record_success(throttle_key)
    resp = RedirectResponse("/admin", status_code=303)
    resp.set_cookie(
        ws.SESSION_COOKIE,
        new.token,
        httponly=True,
        samesite="strict",
        secure=True,
        path="/admin",
        max_age=state.settings.session_ttl_hours * 3600,
    )
    resp.set_cookie(
        "xc_csrf", new.csrf, httponly=False, samesite="strict", secure=True, path="/admin"
    )
    return security_headers(resp)


def _login_error(request: Request, message: str, *, setup: bool = False) -> Response:
    return _render(
        request,
        "login.html",
        {"setup": setup, "action": "/admin/login", "error": message, "csrf": ""},
    )


@router.get("/logout")
async def logout(request: Request) -> Response:
    state = request.app.state.xc
    async with state.sessionmaker() as s:
        await ws.destroy(s, request.cookies.get(ws.SESSION_COOKIE))
        await s.commit()
    resp = RedirectResponse("/admin/login", status_code=303)
    resp.delete_cookie(ws.SESSION_COOKIE, path="/admin")
    resp.delete_cookie("xc_csrf", path="/admin")
    return security_headers(resp)


# =============================================================================
# 总览
# =============================================================================


def _since(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")


@router.get("")
@router.get("/")
async def overview(request: Request) -> Response:
    await require_admin(request)
    state = request.app.state.xc

    async with state.sessionmaker() as s:
        day, week = _since(1), _since(7)

        async def agg(since: str) -> dict[str, Any]:
            row = (
                await s.execute(
                    select(
                        func.count(Run.id),
                        func.coalesce(func.sum(RunUsage.input_tokens), 0),
                        func.coalesce(func.sum(RunUsage.output_tokens), 0),
                    )
                    .select_from(Run)
                    .outerjoin(RunUsage, RunUsage.run_id == Run.id)
                    .where(Run.started_at >= since)
                )
            ).one()
            costs = (
                (
                    await s.execute(
                        select(RunUsage.cost_usd)
                        .select_from(Run)
                        .join(RunUsage, RunUsage.run_id == Run.id)
                        .where(Run.started_at >= since, RunUsage.cost_usd.is_not(None))
                    )
                )
                .scalars()
                .all()
            )
            total = sum((Decimal(c) for c in costs), Decimal(0))
            return {"runs": row[0], "input": row[1], "output": row[2], "cost": total}

        d, w = await agg(day), await agg(week)
        errors = (
            await s.execute(
                select(func.count(Run.id)).where(Run.started_at >= week, Run.status != "ok")
            )
        ).scalar_one()
        runs = await _recent_runs(s, limit=8)

    stats = {
        "today_runs": d["runs"],
        "week_runs": w["runs"],
        "today_cost": _fmt_cost(str(d["cost"])),
        "week_cost": _fmt_cost(str(w["cost"])),
        "today_tokens": d["input"] + d["output"],
        "today_input": d["input"],
        "today_output": d["output"],
        "week_errors": errors,
        "error_rate": round(errors / w["runs"] * 100, 1) if w["runs"] else 0,
    }
    return _render(
        request,
        "overview.html",
        {"stats": stats, "runs": runs, "upstream_configured": state.upstream.configured},
    )


async def _recent_runs(s, *, limit: int, model: str = "", status: str = "") -> list[Any]:
    stmt = (
        select(Run, RunUsage)
        .outerjoin(RunUsage, RunUsage.run_id == Run.id)
        .order_by(Run.started_at.desc())
        .limit(limit)
    )
    if model:
        stmt = stmt.where(Run.model.like(f"%{model}%"))
    if status == "ok":
        stmt = stmt.where(Run.status == "ok")
    elif status == "error":
        stmt = stmt.where(Run.status != "ok")

    out = []
    for run, usage in (await s.execute(stmt)).all():
        out.append(
            {
                "started_at": _fmt_time(run.started_at),
                "model": run.model,
                "status": run.status,
                "error_type": run.error_type,
                "input_tokens": usage.input_tokens if usage else 0,
                "output_tokens": usage.output_tokens if usage else 0,
                "cache_read_tokens": usage.cache_read_tokens if usage else 0,
                "cost_display": _fmt_cost(usage.cost_usd if usage else None),
                "cost_source": usage.cost_source if usage else "unknown",
                "latency_display": f"{run.latency_ms} ms" if run.latency_ms is not None else "—",
            }
        )
    return out


# =============================================================================
# 密钥
# =============================================================================


@router.get("/keys")
async def keys_page(request: Request) -> Response:
    await require_admin(request)
    state = request.app.state.xc
    issued = request.query_params.get("issued")
    name = request.query_params.get("name", "")

    async with state.sessionmaker() as s:
        rows = await auth_svc.list_tokens(s)

    tokens = [
        {
            "name": t.name,
            "kid": t.kid,
            "display_prefix": t.display_prefix,
            "is_active": t.is_active,
            "expired": auth_svc.is_expired(t),
            "last_used_display": _fmt_time(t.last_used_at),
            "created_display": _fmt_time(t.created_at),
        }
        for t in rows
    ]
    csrf = await _ensure_csrf_cookie(request)
    resp = _render(
        request,
        "keys.html",
        {
            "tokens": tokens,
            "csrf": csrf.value,
            "issued": {"plaintext": issued, "name": name} if issued else None,
        },
    )
    csrf.apply(resp)
    return resp


@router.post("/keys/issue")
async def issue_key(
    request: Request,
    name: str = Form(...),
    days: str = Form(default=""),
    csrf_token: str = Form(default=""),
) -> Response:
    await guard_mutation(request, csrf_token)
    state = request.app.state.xc
    expires = auth_svc.parse_expiry(int(days)) if days.strip() else None

    async with state.sessionmaker() as s:
        issued = await auth_svc.issue(s, name=name.strip()[:60] or "未命名", expires_at=expires)
        await s.commit()

    # 明文经 URL 传一次。这不理想（会进浏览器历史），但它只在这一刻有效于展示，
    # 而替代方案（存进服务端 flash）会让明文在库里多活一会儿——两害相权。
    from urllib.parse import quote

    return security_headers(
        RedirectResponse(
            f"/admin/keys?issued={quote(issued.plaintext)}&name={quote(issued.name)}",
            status_code=303,
        )
    )


@router.post("/keys/revoke")
async def revoke_key(
    request: Request, kid: str = Form(...), csrf_token: str = Form(default="")
) -> Response:
    await guard_mutation(request, csrf_token)
    state = request.app.state.xc
    async with state.sessionmaker() as s:
        await auth_svc.revoke(s, kid)
        await s.commit()
    return security_headers(RedirectResponse("/admin/keys", status_code=303))


# =============================================================================
# 调用记录
# =============================================================================


@router.get("/logs")
async def logs_page(request: Request) -> Response:
    await require_admin(request)
    state = request.app.state.xc
    model = request.query_params.get("model", "").strip()
    status = request.query_params.get("status", "").strip()

    async with state.sessionmaker() as s:
        runs = await _recent_runs(s, limit=200, model=model, status=status)
        total = (await s.execute(select(func.count(Run.id)))).scalar_one()

    return _render(
        request,
        "logs.html",
        {"runs": runs, "total": total, "filters": {"model": model, "status": status}},
    )


# =============================================================================
# 设置
# =============================================================================


@router.get("/settings")
async def settings_page(request: Request) -> Response:
    await require_admin(request)
    state = request.app.state.xc

    async with state.sessionmaker() as s:
        raw_key = await setting_svc.get(s, state.keyring, C.SETTING_KEY_OPENROUTER_API_KEY)
        base_url = await setting_svc.get(s, state.keyring, C.SETTING_KEY_OPENROUTER_BASE_URL)

    from ..db import migrate

    csrf = await _ensure_csrf_cookie(request)
    resp = _render(
        request,
        "settings.html",
        {
            "csrf": csrf.value,
            "masked_key": setting_svc.mask(raw_key) if raw_key else "",
            "base_url": base_url or C.OPENROUTER_DEFAULT_BASE_URL,
            "catalog_count": len(state.catalog.all()),
            "catalog_stale": state.catalog.is_stale,
            "data_dir": str(state.settings.data_dir.resolve()),
            "db_revision": migrate.current_revision(state.settings.db_path) or "—",
        },
    )
    csrf.apply(resp)
    return resp


@router.post("/settings/upstream")
async def update_upstream(
    request: Request,
    password: str = Form(...),
    api_key: str = Form(default=""),
    base_url: str = Form(default=""),
    csrf_token: str = Form(default=""),
) -> Response:
    await guard_mutation(request, csrf_token)
    state = request.app.state.xc

    async with state.sessionmaker() as s:
        admin = await ws.get_admin(s)
        # 二次确认身份：这个表单一旦被跨站提交，付费 key 就会被送到攻击者的服务器。
        # CSRF 三层之外再加一道，因为这是全后台后果最严重的一个操作。
        if admin is None or not ws.verify_password(admin.password_hash, password):
            raise Denied("密码不正确，未做任何修改。")

        if base_url.strip():
            try:
                checked = check_upstream_url(base_url.strip())
            except UnsafeUpstreamURL as e:
                raise Denied(f"中转地址被拒绝：{e}") from e
            await setting_svc.set_(s, state.keyring, C.SETTING_KEY_OPENROUTER_BASE_URL, checked.url)

        if api_key.strip():
            await setting_svc.set_(
                s, state.keyring, C.SETTING_KEY_OPENROUTER_API_KEY, api_key.strip()
            )
        await s.commit()

    from ..app import load_upstream

    await load_upstream(state)
    up = state.upstream.config
    if up is not None:
        await state.catalog.refresh(state.upstream.client(), up.api_key)

    return security_headers(RedirectResponse("/admin/settings", status_code=303))


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
            _render(request, "_test_result.html", {"ok": False, "message": "还没有配置上游 key。"})
        )

    ok = await state.catalog.refresh(state.upstream.client(), cfg.api_key)
    if not ok:
        # 错误文本可能带完整 URL，脱敏后再回显
        from ..errors import redact

        return security_headers(
            _render(
                request,
                "_test_result.html",
                {"ok": False, "message": redact(state.catalog.last_error or "未知错误")},
            )
        )

    models = state.catalog.all()
    return security_headers(
        _render(
            request,
            "_test_result.html",
            {
                "ok": True,
                "count": len(models),
                "native": sum(1 for m in models if m.supports_native_schema),
            },
        )
    )


# =============================================================================
# 装配
# =============================================================================


def mount(app) -> None:
    """挂载后台。静态文件内嵌进 wheel，不走 CDN——离线可用是硬约束。"""
    app.include_router(router)
    app.mount("/admin/static", StaticFiles(directory=str(HERE / "static")), name="xc-static")


# =============================================================================
# Agent
# =============================================================================


def _tier_options() -> list[Any]:
    """表单里可选的档位。

    只列已实现的：T1 需要先有"strict=True 会静默把可选字段提升为必填"的提示，
    没有它就开放 T1 等于让用户在不知情的情况下承担对齐税。
    """
    from ..core.guarantee import AVAILABLE_TIERS, TIER_INFO

    return [SimpleNamespace(value=t.value, **TIER_INFO[t]) for t in AVAILABLE_TIERS]


def _lint_ctx(schema_text: str, tier: str) -> dict[str, Any]:
    from ..core.schema_guard import SchemaRejected, validate_schema
    from ..core.schema_lint import lint, summarize

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


@router.get("/agents")
async def agents_page(request: Request) -> Response:
    await require_admin(request)
    state = request.app.state.xc
    from ..core.guarantee import TIER_INFO
    from ..services import agent as agent_svc

    async with state.sessionmaker() as s:
        pairs = await agent_svc.list_all(s)

    rows = []
    for row, ver in pairs:
        spec = json.loads(ver.spec_json) if ver else {}
        tier = ver.tier if ver else "—"
        rows.append(
            SimpleNamespace(
                slug=row.slug,
                name=row.name,
                description=row.description,
                is_active=row.is_active,
                model=spec.get("model", "—"),
                version=ver.version if ver else 0,
                tier=tier,
                tier_desc=TIER_INFO.get(C.Tier(tier), {}).get("content", "") if ver else "",
                structured=bool(ver and ver.out_schema),
            )
        )
    return _render(request, "agents.html", {"agents": rows})


def _empty_form() -> Any:
    return SimpleNamespace(
        slug="", name="", description="", instructions="", model="", schema="", tier="T2", retries=2
    )


async def _model_choices(state: Any) -> tuple[list[Any], int]:
    models = state.catalog.all()
    return models, sum(1 for m in models if m.supports_native_schema)


@router.get("/agents/new")
async def agent_new(request: Request) -> Response:
    await require_admin(request)
    models, native = await _model_choices(request.app.state.xc)
    csrf = await _ensure_csrf_cookie(request)
    resp = _render(
        request,
        "agent_form.html",
        {
            "is_new": True,
            "agent": None,
            "form": _empty_form(),
            "action": "/admin/agents/save",
            "csrf": csrf.value,
            "models": models,
            "native_count": native,
            "tiers": _tier_options(),
            "versions": [],
            "hints": [],
            "error": None,
            "saved": None,
        },
    )
    csrf.apply(resp)
    return resp


@router.get("/agents/{slug}")
async def agent_edit(slug: str, request: Request) -> Response:
    await require_admin(request)
    state = request.app.state.xc
    from ..services import agent as agent_svc

    async with state.sessionmaker() as s:
        resolved = await agent_svc.resolve(s, slug)
        vers = await agent_svc.versions(s, resolved.agent_id)
        version_rows = [
            SimpleNamespace(
                version=v.version,
                tier=v.tier,
                created=v.created_at[:19],
                current=v.id == resolved.version_id,
            )
            for v in vers
        ]

    spec = json.loads(resolved.spec_json)
    form = SimpleNamespace(
        slug=resolved.slug,
        name=resolved.name,
        description=resolved.description or "",
        instructions=spec.get("instructions", ""),
        model=spec.get("model", ""),
        schema=json.dumps(json.loads(resolved.out_schema), ensure_ascii=False, indent=2)
        if resolved.out_schema
        else "",
        tier=resolved.tier.value if resolved.out_schema else "",
        retries=spec.get("retries", 2),
    )

    models, native = await _model_choices(state)
    csrf = await _ensure_csrf_cookie(request)
    resp = _render(
        request,
        "agent_form.html",
        {
            "is_new": False,
            "agent": resolved,
            "form": form,
            "action": "/admin/agents/save",
            "csrf": csrf.value,
            "models": models,
            "native_count": native,
            "tiers": _tier_options(),
            "versions": version_rows,
            "error": None,
            "saved": None,
            **_lint_ctx(form.schema, form.tier),
        },
    )
    csrf.apply(resp)
    return resp


@router.post("/agents/save")
async def agent_save(
    request: Request,
    slug: str = Form(...),
    name: str = Form(...),
    description: str = Form(default=""),
    instructions: str = Form(...),
    model: str = Form(...),
    output_schema: str = Form(default=""),
    tier: str = Form(default=""),
    retries: int = Form(default=2),
    csrf_token: str = Form(default=""),
) -> Response:
    await guard_mutation(request, csrf_token)
    state = request.app.state.xc
    from ..errors import XingchaError
    from ..services import agent as agent_svc

    native_ok = state.catalog.supports_native_schema(model)
    try:
        async with state.sessionmaker() as s:
            result = await agent_svc.save(
                s,
                slug=slug.strip(),
                name=name.strip(),
                description=description.strip() or None,
                instructions=instructions,
                model=model.strip(),
                schema_text=output_schema,
                requested_tier=C.Tier(tier) if tier else None,
                capabilities=None,
                retries=max(0, min(5, retries)),
                native_ok=native_ok,
            )
            await s.commit()
    except XingchaError as e:
        # 表单错误回到表单页并保留用户填的内容——跳到一个错误页会让人白填一遍
        models, native = await _model_choices(state)
        csrf = await _ensure_csrf_cookie(request)
        form = SimpleNamespace(
            slug=slug,
            name=name,
            description=description,
            instructions=instructions,
            model=model,
            schema=output_schema,
            tier=tier,
            retries=retries,
        )
        resp = _render(
            request,
            "agent_form.html",
            {
                "is_new": True,
                "agent": None,
                "form": form,
                "action": "/admin/agents/save",
                "csrf": csrf.value,
                "models": models,
                "native_count": native,
                "tiers": _tier_options(),
                "versions": [],
                "hints": [],
                "error": e.message,
                "saved": None,
            },
        )
        csrf.apply(resp)
        return resp

    return security_headers(RedirectResponse(f"/admin/agents/{result.slug}", status_code=303))


@router.post("/agents/{slug}/rollback")
async def agent_rollback(
    slug: str, request: Request, version: int = Form(...), csrf_token: str = Form(default="")
) -> Response:
    await guard_mutation(request, csrf_token)
    state = request.app.state.xc
    from ..services import agent as agent_svc

    async with state.sessionmaker() as s:
        resolved = await agent_svc.resolve(s, slug)
        await agent_svc.rollback(s, resolved.agent_id, version)
        await s.commit()
    return security_headers(RedirectResponse(f"/admin/agents/{slug}", status_code=303))


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
    return security_headers(_render(request, "_lint.html", _lint_ctx(output_schema, tier)))
