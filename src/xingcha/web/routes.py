"""管理后台。

安全约束集中在 :func:`guard_mutation`——**每一个改状态的请求都必须经过它**。
后台暴露在公网上，而它里面有一个能改写上游 base_url 的表单：一次成功的 CSRF
就等于把付费 key 送到攻击者的服务器。所以三层叠加：SameSite=Strict cookie、
double-submit token、Origin/Sec-Fetch-Site 校验。
"""

from __future__ import annotations

import hashlib
import json
import logging
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import cache
from itertools import zip_longest
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
from ..services import trace_targets
from ..services import websession as ws

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))


# =============================================================================
# 静态资源的版本号
# =============================================================================


@cache
def _asset_digest(name: str, _stamp: tuple[int, int]) -> str:
    """静态资源内容的短哈希。

    ``_stamp`` 是 ``(mtime_ns, size)``，只用来做缓存键：**文件一改，键就变，
    哈希自动重算。** 不这么做的话进程内只算一次，而我们对静态资源发的是
    ``immutable`` 一年缓存——URL 不变 + 浏览器永久缓存 = 改了 CSS 却永远看不到，
    而且看起来像"改的地方没生效"。生产上无所谓（部署就是新进程），开发时能耗掉
    很长时间才想到是缓存。实际踩过。
    """
    return hashlib.sha256((HERE / "static" / name).read_bytes()).hexdigest()[:8]


def asset(name: str) -> str:
    """静态资源的带版本 URL，形如 ``/admin/static/style.css?v=1a2b3c4d``。

    版本号是**文件内容的哈希**，不是应用版本号：开发期改一行 CSS 而版本号没变的
    情况太常见，而症状恰恰是最难认的那种——新模板配旧样式表，页面上的东西看起来
    "错位挤在一起"，而 HTML、CSS、Python 每一份单独看都是对的。

    有了内容哈希，URL 随内容变，于是可以放心声明 ``immutable`` 长缓存：
    平时零请求，升级后必取新的。这直接服务于"升级对用户透明"——用户不该需要知道
    "改完样式要硬刷新"这件事。

    每次渲染一个 ``stat()``（三个小文件），哈希本身由 :func:`_asset_digest` 缓存。
    """
    st = (HERE / "static" / name).stat()
    return f"/admin/static/{name}?v={_asset_digest(name, (st.st_mtime_ns, st.st_size))}"


templates.env.globals["asset"] = asset


class _VersionedStatic(StaticFiles):
    """静态文件 + 长缓存。

    URL 带内容哈希，所以 ``immutable`` 是**成立的**：同一个 URL 的内容永远不变。
    不加这个头的话浏览器只能按启发式缓存——既可能每次都revalidate（白跑请求），
    也可能几天不问一次（升级后看到旧样式）。两种都不是我们想要的。
    """

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp


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
        # 空串 = 不写 data-theme，交给 CSS 的 prefers-color-scheme。
        # 从 cookie 读，所以首屏渲染出来就是对的，不会闪一下再换。
        "theme": read_theme(request),
        # 切换器要高亮"当前选的是哪个"，而 theme 把 system 折叠成了空串——
        # 那个折叠是给 data-theme 用的，不能拿来做 UI 状态。
        "theme_choice": request.cookies.get(C.THEME_COOKIE, "system"),
        # 切换表单的 CSRF。占位，下面按"这一页自己有没有 csrf"决定实际取值。
        "csrf_theme": "",
        # 密码长度下限：前端 minlength 与后端校验必须是同一个值，否则用户会被一个
        # 说不清的错误挡住（前端放过、后端拒绝）。
        "min_password_len": C.MIN_ADMIN_PASSWORD_LEN,
    }
    merged = {**base, **ctx}
    # 侧栏那个主题表单的令牌：**优先用这一页自己签发的**，回落到请求里的 cookie。
    #
    # 只读 cookie 是不够的，而且会真的坏：xc_csrf 没有 max_age（浏览器会话级），
    # 而 xc_session 有 7 天——重启浏览器之后会话还在、CSRF cookie 已经没了，
    # 于是侧栏表单拿到空串，点主题直接 403。而同一页里那些自己传了 csrf 的表单
    # 照常工作，所以症状是"只有切主题不好使"。
    merged["csrf_theme"] = merged.get("csrf") or request.cookies.get("xc_csrf", "")
    resp = templates.TemplateResponse(request, template, merged)
    return security_headers(resp)  # type: ignore[return-value]


def _fmt_time(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso).strftime("%m-%d %H:%M")
    except ValueError:
        return iso[:16]


#: 费用来源的人话解释。**实价与估价必须能一眼分开**——把两者显示成同一个样子，
#: 看账单的人会以为目录估价就是要付的钱，而两者能差几百倍。
_COST_HINT = {
    C.CostSource.UPSTREAM.value: "上游报的实际费用",
    C.CostSource.CATALOG.value: "按模型目录价预估（非实际账单）",
    C.CostSource.GENAI_PRICES.value: "按 genai-prices 预估（非实际账单）",
    C.CostSource.UNKNOWN.value: "目录里没有这个模型的价格，无法定价",
}


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
        has_db_password = await ws.has_password(s)
        env_managed = ws.env_password_in_effect(
            "x" if has_db_password else None,
            request.app.state.xc.settings.admin_password,
        )
        setup = not has_db_password and not env_managed
    if await current_session(request) is not None:
        return RedirectResponse("/admin", status_code=303)

    csrf = await _ensure_csrf_cookie(request)
    resp = _render(
        request,
        "login.html",
        {
            "setup": setup,
            "env_managed": env_managed,
            "action": "/admin/login",
            "error": None,
            "csrf": csrf.value,
            # **登录页永远是暗色**，不跟随主题。银河是夜景——"亮色银河"是个矛盾：
            # 白底上的星点不像星，像页面没渲染干净。与其做一套注定难看的亮色，
            # 不如让这一页只有一种样子。后台各页照旧跟随用户的选择。
            "theme": "dark",
        },
    )
    csrf.apply(resp)
    return resp


def read_theme(request: Request) -> str:
    """当前主题，用于 ``<html data-theme="...">``。

    返回 ``""``（跟随系统）、``"light"`` 或 ``"dark"``。cookie 里是别的值就当没设——
    那一格是用户可写的，不能直接塞进 HTML 属性。
    """
    value = request.cookies.get(C.THEME_COOKIE, "system")
    if value not in C.THEMES or value == "system":
        return ""
    return value


def cookie_secure(request: Request) -> bool:
    """会话与 CSRF cookie 要不要带 ``Secure``。**跟随请求自身的协议，不写死。**

    写死 ``True`` 的代价：纯 HTTP 部署下浏览器**直接丢掉** cookie，症状是"密码
    输对了却一直跳回登录页"，而服务端日志显示登录成功、会话已签发——两边看起来
    都正常，是最难查的一类。（``localhost`` 例外：浏览器把它当安全上下文，所以
    本机开发看不出问题，只有换成局域网 IP 才炸。）

    写死 ``False`` 的代价：HTTPS 部署下，攻击者可以把受害者引到同域的 http 链接，
    让浏览器把凭证明文发出来。

    所以只有一个正确答案——问这次请求本身。直连时 ``request.url.scheme`` 就是
    真实协议。

    **反代后面需要额外一步**：uvicorn 默认不读 ``X-Forwarded-Proto``（读了就等于
    信任任何人伪造的那个头），所以放了反代之后要显式开 ``proxy_headers`` 并把
    ``forwarded_allow_ips`` 限定到反代的地址。当前部署是直连 HTTP，没开。
    """
    return request.url.scheme == "https"


@dataclass
class _Csrf:
    value: str
    fresh: bool
    secure: bool = True
    max_age: int = 0

    def apply(self, resp: Response) -> None:
        if self.fresh:
            resp.set_cookie(
                "xc_csrf",
                self.value,
                httponly=False,  # 表单要读它；它本身不是凭证，只是"你能读到本站页面"的证明
                samesite="strict",
                secure=self.secure,
                path="/admin",
                # **必须和会话同寿**。此前没有 max_age，也就是浏览器会话级：
                # 关掉浏览器再打开，xc_session 还在（它有 7 天），xc_csrf 已经没了。
                # 于是任何"从 cookie 里取令牌"的表单都会 403，而同一页里自己签发
                # 令牌的表单照常工作——症状是"只有某几个按钮不好使"。
                max_age=self.max_age or None,
            )


async def _ensure_csrf_cookie(request: Request) -> _Csrf:
    existing = request.cookies.get("xc_csrf")
    if existing:
        return _Csrf(existing, fresh=False)
    import secrets

    return _Csrf(
        secrets.token_urlsafe(32),
        fresh=True,
        secure=cookie_secure(request),
        max_age=request.app.state.xc.settings.session_ttl_hours * 3600,
    )


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

        env_password = state.settings.admin_password
        env_managed = ws.env_password_in_effect(admin.password_hash, env_password)
        # 环境变量生效时**不走首次设密**：走了的话用户会以为自己设了个新密码，
        # 而下一次登录仍然按环境变量校验——一个"我明明改了"却毫无效果的状态。
        setup = not admin.password_hash and not env_managed
        if setup:
            if len(password) < C.MIN_ADMIN_PASSWORD_LEN:
                return _login_error(
                    request, f"密码至少 {C.MIN_ADMIN_PASSWORD_LEN} 位。", setup=True
                )
            if password != confirm:
                return _login_error(request, "两次输入的密码不一致。", setup=True)
            admin.password_hash = ws.hash_password(password)
            log.info("已设置管理员密码")
        else:
            if not ws.verify_admin_password(admin.password_hash, password, env_password):
                _throttle.record_failure(throttle_key)
                # 不区分"用户不存在"与"密码错误"，也不提示剩余次数
                return _login_error(request, "密码不正确。")
            if not env_managed and admin.password_hash and ws.needs_rehash(admin.password_hash):
                admin.password_hash = ws.hash_password(password)

        new = await ws.create(s, admin.id, ttl_hours=state.settings.session_ttl_hours)
        await s.commit()

    _throttle.record_success(throttle_key)
    resp = RedirectResponse("/admin", status_code=303)
    secure = cookie_secure(request)
    resp.set_cookie(
        ws.SESSION_COOKIE,
        new.token,
        httponly=True,
        samesite="strict",
        secure=secure,
        path="/admin",
        max_age=state.settings.session_ttl_hours * 3600,
    )
    resp.set_cookie(
        "xc_csrf",
        new.csrf,
        httponly=False,
        samesite="strict",
        secure=secure,
        path="/admin",
        # 与上面的会话 cookie 同寿，理由见 _Csrf.apply
        max_age=state.settings.session_ttl_hours * 3600,
    )
    return security_headers(resp)


def _login_error(request: Request, message: str, *, setup: bool = False) -> Response:
    # `env_managed` 也要传：漏了的话失败重渲染时"密码由环境变量托管"那行提示会消失，
    # 于是用户在最需要这条信息的时刻（刚输错）看不到它。
    env_managed = ws.env_password_in_effect(None, request.app.state.xc.settings.admin_password)
    return _render(
        request,
        "login.html",
        {
            "setup": setup,
            "env_managed": env_managed,
            "action": "/admin/login",
            "error": message,
            "csrf": "",
            "theme": "dark",  # 与登录页一致，见 login_page
        },
    )


@router.post("/theme")
async def set_theme(
    request: Request,
    value: str = Form(...),
    back: str = Form(default="/admin"),
    csrf_token: str = Form(default=""),
) -> Response:
    """切换主题。

    POST 而不是 GET：它改状态。改的只是一个显示偏好，但"改状态的 GET"会被浏览器
    预取、被历史记录重放，而且会让 CSRF 那套纪律出现一个例外——例外比这个功能贵。

    ``back`` 必须是站内路径。不校验的话这就是一个开放重定向：
    ``/admin/theme`` 带上 ``back=https://坏人.com`` 就能把已登录的管理员送出去，
    而链接看起来完全是自己站里的。
    """
    await guard_mutation(request, csrf_token)
    if value not in C.THEMES:
        raise Denied(f"未知的主题：{value}")

    target = back if back.startswith("/admin") and "//" not in back else "/admin"
    resp = security_headers(RedirectResponse(target, status_code=303))
    resp.set_cookie(
        C.THEME_COOKIE,
        value,
        httponly=False,  # 不是凭证，只是一个显示偏好
        samesite="strict",
        secure=cookie_secure(request),
        path="/admin",
        max_age=400 * 24 * 3600,
    )
    return resp


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
                "cost_hint": _COST_HINT.get(usage.cost_source if usage else "unknown", "来源未知"),
                "latency_display": f"{run.latency_ms} ms" if run.latency_ms is not None else "—",
            }
        )
    return out


# =============================================================================
# 密钥
# =============================================================================


@router.get("/keys")
async def keys_page(request: Request) -> Response:
    session = await require_admin(request)
    state = request.app.state.xc
    # 一次性取走。取走即删，所以刷新页面不会再显示——"这是唯一一次看到明文"
    # 这句话由此才成立。明文从不进 URL、不进浏览器历史、不落盘。
    # 明文与用途名用换行分隔存成一条：两条 flash 会出现"取了一条另一条还在"的
    # 中间态，而这里要的是原子的一次性。
    flashed = state.flash.take(f"{session.id}:issued_key")
    issued, name = (
        (flashed.split("\n", 1) if "\n" in flashed else [flashed, ""]) if flashed else (None, "")
    )

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

    session = await current_session(request)
    async with state.sessionmaker() as s:
        issued = await auth_svc.issue(s, name=name.strip()[:60] or "未命名", expires_at=expires)
        await s.commit()

    # **明文不进 URL。**
    #
    # 原先是 `303 → /admin/keys?issued=sk-xc-...`，代价是明文进浏览器历史、留在
    # 地址栏（截图/录屏/肩窥）、进 Referer，而且**刷新就重现**——那让页面上
    # "这是唯一一次看到明文"变成一句假话。
    #
    # 当时的注释认为替代方案会让明文在库里多活一会儿，那是个假两难：单 worker 是
    # 断言过的硬约束，进程内存里做一次性存取就够了，一次都不落盘。见 web/flash.py。
    assert session is not None  # require_admin 已在 guard_mutation 里过了
    state.flash.put(f"{session.id}:issued_key", f"{issued.plaintext}\n{issued.name}")
    return security_headers(RedirectResponse("/admin/keys", status_code=303))


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

    from ..db import migrate

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
    csrf = await _ensure_csrf_cookie(request)
    ctx = await _settings_ctx(request, **errors)
    resp = _render(request, "settings.html", {"csrf": csrf.value, **ctx})
    csrf.apply(resp)
    return resp


@router.get("/settings")
async def settings_page(request: Request) -> Response:
    await require_admin(request)
    return await _render_settings(request)


def _form_error(request: Request, message: str) -> Response:
    """表单里的一条错误，**只换结果区那一小块**。

    整页重渲染会把用户填的东西清空——而这个表单要填名字、地址、key、密码四样，
    最常错的是地址（少了或多了 ``/v1``）。清空一次就等于罚他重输四遍。
    """
    return security_headers(
        _render(request, "_upstream_check.html", {"ok": False, "message": message})
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
    from ..services import providers as provider_svc
    from ..services import upstream_env as ue

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
    from ..services import upstream_env as ue

    state = request.app.state.xc

    try:
        checked = check_upstream_url(base_url.strip())
    except UnsafeUpstreamURL as e:
        return security_headers(
            _render(request, "_upstream_check.html", {"ok": False, "message": f"地址被拒绝：{e}"})
        )
    if not api_key.strip():
        return security_headers(
            _render(request, "_upstream_check.html", {"ok": False, "message": "API key 不能为空。"})
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
        _render(
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
    from ..services import providers as provider_svc

    state = request.app.state.xc
    async with state.sessionmaker() as s:
        await provider_svc.remove(s, state.keyring, name)
        await s.commit()
    return security_headers(RedirectResponse("/admin/upstreams", status_code=303))


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
    from ..app import load_tracing

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
    app.mount("/admin/static", _VersionedStatic(directory=str(HERE / "static")), name="xc-static")


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


def _prompting_from_form(raw: Any) -> Any:
    """表单 → :class:`builder.Prompting`。校验在 builder 里，这里只负责取值。

    示例是不定组数的，所以按 ``getlist`` 收而不是逐个声明 Form 参数——组数由前端
    决定，后端写死几组就等于给"再加一组"设了一个看不见的上限。
    """
    from ..core import builder

    users = raw.getlist("ex_user")
    assistants = raw.getlist("ex_assistant")
    # 两个列表按位置配对。长度不等只可能是前端出了 bug，短的那边补空串，
    # 让 validate_prompting 报"要成对"，而不是在这里 IndexError。
    pairs = [
        builder.Example(u, a)
        for u, a in zip_longest(users, assistants, fillvalue="")
        if (u or a).strip()
    ]
    return builder.validate_prompting(str(raw.get("user_template") or ""), pairs)


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
        slug="",
        name="",
        description="",
        instructions="",
        model="",
        schema="",
        tier="T2",
        retries=2,
        **_settings_view({}),
    )


async def _model_choices(state: Any) -> tuple[list[Any], int]:
    models = state.catalog.all()
    return models, sum(1 for m in models if m.supports_native_schema)


async def _form_shell(request: Request) -> dict[str, Any]:
    """三个 handler（new / edit / save 出错回填）共用的表单上下文。

    写三份的话，加一个分区就要改三处——而漏掉一处的症状是"新建页有这个字段、
    编辑页没有"，一种很晚才会被发现的不一致。
    """
    from ..core import builder

    state = request.app.state.xc
    models, native = await _model_choices(state)
    tracing = state.tracing
    return {
        "models": models,
        "native_count": native,
        "tiers": _tier_options(),
        "model_settings": builder.model_settings_fields(),
        "capabilities": builder.form_capabilities(),
        # 可观测那一栏要知道地址配了没：没配就不该给一个勾了没用的开关
        "trace_endpoint": tracing.endpoint if tracing is not None else "",
        "trace_include_content": tracing.include_content if tracing is not None else False,
    }


def _settings_view(spec: dict[str, Any]) -> dict[str, Any]:
    """提示词组装/模型参数/能力/可观测 几块的回填值。

    **这里逐项挑键，所以 form_view 新增字段时必须同步。** 漏掉一项不会报错——
    模板里读到的是 Jinja 的 Undefined，渲染成空串，于是编辑页那一栏看起来"从来
    没填过"，一保存就把用户设过的东西清掉。刚在 user_template 上踩过一次。
    """
    from ..core import builder

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
    }


@router.get("/agents/new")
async def agent_new(request: Request) -> Response:
    await require_admin(request)
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
            "versions": [],
            "hints": [],
            "error": None,
            "saved": None,
            **await _form_shell(request),
        },
    )
    csrf.apply(resp)
    return resp


def _take_saved(request: Request, session: Any) -> Any:
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


@router.get("/agents/{slug}")
async def agent_edit(slug: str, request: Request) -> Response:
    session = await require_admin(request)
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
        **_settings_view(spec),
    )

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
            "versions": version_rows,
            **await _form_shell(request),
            "error": None,
            # 取走上一次保存的结果（经 flash 跨过 303）。一次性：刷新页面不再提示。
            "saved": _take_saved(request, session),
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
    from ..core import builder
    from ..errors import XingchaError
    from ..services import agent as agent_svc

    # 模型参数与能力用 ms_* / cap_* 前缀收，而不是逐个声明 Form 参数：
    # 字段清单由官方 schema 驱动（见 builder.FORM_MODEL_SETTINGS），逐个声明就等于
    # 把那份清单抄第二遍，而两份清单迟早会不一致。
    raw = await request.form()
    settings_raw = {
        field: str(raw.get(f"ms_{field}") or "") for field, _, _ in builder.model_settings_fields()
    }
    caps = [name for name, _, _ in builder.form_capabilities() if raw.get(f"cap_{name}")]
    if raw.get("instrument"):
        # 可观测就是 Instrumentation 这个 capability——不是 AgentSpec 的顶层字段
        caps.append(builder.CAPABILITY_INSTRUMENTATION)

    native_ok = state.catalog.supports_native_schema(model)
    try:
        prompting = _prompting_from_form(raw)
        model_settings = builder.model_settings_from_form(settings_raw)
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
                capabilities=caps or None,
                model_settings=model_settings or None,
                retries=max(0, min(5, retries)),
                native_ok=native_ok,
                prompting=prompting,
            )
            await s.commit()
    except XingchaError as e:
        # 表单错误回到表单页并保留用户填的内容——跳到一个错误页会让人白填一遍。
        # 模型参数与能力也要保留：只回填前半截的话，用户会以为那些设置没生效。
        csrf = await _ensure_csrf_cookie(request)
        filled = {k: v for k, v in settings_raw.items() if v.strip()}
        form = SimpleNamespace(
            slug=slug,
            name=name,
            description=description,
            instructions=instructions,
            model=model,
            schema=output_schema,
            tier=tier,
            retries=retries,
            settings=settings_raw,
            has_settings=bool(filled),
            settings_count=len(filled),
            capabilities=set(caps),
            instrumented=builder.CAPABILITY_INSTRUMENTATION in caps,
            user_template=str(raw.get("user_template") or ""),
            # 回填用户填的原文，而不是 validate_prompting 清洗过的版本：报错时把人
            # 填的东西改掉，会让他对着一个自己没写过的表单找错。
            examples=[
                SimpleNamespace(user=u, assistant=a)
                for u, a in zip_longest(
                    raw.getlist("ex_user"), raw.getlist("ex_assistant"), fillvalue=""
                )
            ],
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
                "versions": [],
                "hints": [],
                "error": e.message,
                "saved": None,
                **await _form_shell(request),
            },
        )
        csrf.apply(resp)
        return resp

    # 保存结果经 flash 带过重定向。
    #
    # 不带的话编辑页的 `{% if saved %}已保存为 v… %}` 那一块**永远不显示**——用户
    # 保存完看不到任何确认，更要紧的是同一块里的 `tier_note` 也一起丢了：
    # "你请求了 T1，但这个模型不支持原生约束，已降级到 T2" 这句话是静默消失的，
    # 而两档的失败形态完全不同。
    session = await current_session(request)
    if session is not None:
        state.flash.put(f"{session.id}:saved_agent", f"{result.version}\n{result.tier_note or ''}")
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


def _chain_rows(messages: list[Any]) -> list[Any]:
    """``all_messages()`` → 面板上的一行一条。

    渲染的是**上游实际收发的东西**，不是照表单重建的"应该发什么"。两者分叉的
    那一刻正好是最需要看这个面板的时候，所以重建版没有价值。
    """
    rows: list[Any] = []
    for msg in messages:
        is_req = getattr(msg, "kind", "") == "request"
        # 指令挂在 request 上而不是单独一条消息：单拎出来，才看得见系统提示词
        # 与调用方追加的那段拼在一起之后长什么样。
        instructions = getattr(msg, "instructions", None)
        if is_req and instructions:
            rows.append(
                SimpleNamespace(role="instructions", label="系统指令", text=instructions, meta="")
            )
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
    from ..core import builder
    from ..core.builder import BuildOptions
    from ..core.schema_guard import SchemaRejected, validate_schema
    from ..errors import XingchaError
    from ..services import run as run_svc

    raw = await request.form()
    probe = str(raw.get("test_input") or "").strip()

    def failed(message: str) -> Response:
        return security_headers(
            _render(request, "_agent_test.html", {"ok": False, "message": message})
        )

    if not probe:
        return failed("先填一段测试输入——它就是调用方会发来的那条 user 消息。")
    if state.provider is None:
        return failed("还没有配置上游 key。到「上游」页配好之后再试。")

    try:
        prompting = _prompting_from_form(raw)
        inlined = (
            validate_schema(str(raw.get("output_schema") or ""))
            if str(raw.get("output_schema") or "").strip()
            else None
        )
        tier_raw = str(raw.get("tier") or "")
        model = str(raw.get("model") or "").strip()
        if not model:
            return failed("先选一个模型。")

        from ..core.guarantee import resolve_tier

        choice = resolve_tier(
            C.Tier(tier_raw) if tier_raw else None,
            has_schema=inlined is not None,
            native_ok=state.catalog.supports_native_schema(model),
        )
        settings_raw = {
            f: str(raw.get(f"ms_{f}") or "") for f, _, _ in builder.model_settings_fields()
        }
        caps = [n for n, _, _ in builder.form_capabilities() if raw.get(f"cap_{n}")]
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
        return failed(str(getattr(e, "message", e)))

    conv = run_svc.apply_prompting(
        run_svc.to_conversation([{"role": "user", "content": probe}]), rt.prompting
    )

    started = time.monotonic()
    try:
        outcome = await run_svc.execute(rt, conv=conv, run_timeout=state.settings.run_timeout)
    except XingchaError as e:
        # 失败也把链路渲染出来：**看得见模型到底收到了什么**，才知道是提示词的问题
        # 还是 schema 的问题。只显示一句"失败了"等于什么都没说。
        return security_headers(
            _render(
                request,
                "_agent_test.html",
                {
                    "ok": False,
                    "message": e.message,
                    "rows": _chain_rows(getattr(e, "messages", []) or []),
                    "elapsed": f"{time.monotonic() - started:.1f}",
                },
            )
        )

    from .runlog_mw import price

    cost, source = price(
        state.catalog,
        outcome.model_id,
        {
            "input_tokens": outcome.input_tokens,
            "output_tokens": outcome.output_tokens,
            "cache_read_tokens": outcome.cache_read_tokens,
        },
    )
    return security_headers(
        _render(
            request,
            "_agent_test.html",
            {
                "ok": True,
                "rows": _chain_rows(outcome.messages),
                "output": outcome.content,
                "tier": choice.tier.value,
                "tier_note": choice.reason,
                "elapsed": f"{time.monotonic() - started:.1f}",
                "input_tokens": outcome.input_tokens,
                "output_tokens": outcome.output_tokens,
                "requests": outcome.requests,
                "retries": outcome.schema_retries,
                "violations": outcome.schema_violations,
                "cost": _fmt_cost(str(cost)) if cost is not None else "—",
                "cost_source": source,
            },
        )
    )


@router.get("/agents/{slug}/export")
async def agent_export(slug: str, request: Request) -> Response:
    """把 bundle 打成 zip 下载。

    在内存里打包而不是落临时文件：这些文件很小，而临时文件要考虑清理、并发同名、
    以及"进程被 kill 之后残留"——为一个几 KB 的下载引入那些不值得。
    """
    import io
    import zipfile

    await require_admin(request)
    state = request.app.state.xc
    from ..core import exporter
    from ..services import agent as agent_svc

    async with state.sessionmaker() as s:
        a = await agent_svc.resolve(s, slug)

    with tempfile.TemporaryDirectory() as tmp:
        bundle = exporter.export(
            slug=a.slug,
            name=a.name,
            version=a.version,
            tier=a.tier,
            spec=json.loads(a.spec_json),
            out_schema=json.loads(a.out_schema) if a.out_schema else None,
            dest=Path(tmp),
        )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in bundle.files:
                zf.write(bundle.directory / name, arcname=f"{a.slug}/{name}")

    return security_headers(
        Response(
            content=buf.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{a.slug}-v{a.version}.zip"'},
        )
    )


# =============================================================================
# 上游切换
# =============================================================================


async def _upstream_context(request: Request, *, error: str | None = None) -> dict[str, Any]:
    from ..services import agent as agent_svc
    from ..services import providers as provider_svc
    from ..services import upstream_env as ue

    state = request.app.state.xc
    async with state.sessionmaker() as s:
        active_ref = await setting_svc.get(s, state.keyring, C.SETTING_KEY_UPSTREAM_ACTIVE_ENV)
        raw_key = await setting_svc.get(s, state.keyring, C.SETTING_KEY_OPENROUTER_API_KEY)
        base_url = await setting_svc.get(s, state.keyring, C.SETTING_KEY_OPENROUTER_BASE_URL)
        agents = await agent_svc.list_all(s)
        saved = await provider_svc.list_all(s, state.keyring)

    default_key, default_base = ue.default_from_env()

    # 切换列表 = 环境里的默认那一对 + 扫到的厂商 key + 用户自己加的。
    #
    # **默认那一对必须在列表里**，否则切到别家之后回不来——它没有"厂商变量名"，
    # 此前也就没有对应的一行，只能靠重新写 .env + 重启。实际撞过。
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


async def _render_upstreams(request: Request, *, error: str | None = None) -> Response:
    csrf = await _ensure_csrf_cookie(request)
    resp = _render(
        request,
        "upstreams.html",
        {**await _upstream_context(request, error=error), "csrf": csrf.value},
    )
    csrf.apply(resp)
    return resp


@router.get("/upstreams")
async def upstreams_page(request: Request) -> Response:
    await require_admin(request)
    return await _render_upstreams(request)


async def _resolve_candidate(request: Request, source: str, ref: str) -> tuple[str, str]:
    """把（来源，标识）解析成 ``(api_key, 默认 base_url)``。

    两种来源必须走同一个出口，否则"探测用环境里的、切换用库里的"这类错配只会在
    某一条路径上炸——而两条路径的代码看起来一模一样。
    """
    from ..services import providers as provider_svc
    from ..services import upstream_env as ue

    if source == "saved":
        state = request.app.state.xc
        async with state.sessionmaker() as s:
            got = await provider_svc.get(s, state.keyring, ref)
        if got is None:
            raise Denied(f"没有名为 {ref} 的供应商——是不是已经删掉了？")
        return got.api_key, got.base_url

    if source != "env":
        raise Denied(f"未知的来源：{source}")
    api_key = ue.read_key(ref)
    if not api_key:
        raise Denied(f"环境变量 {ref} 现在读不到值——是不是已经从 .env 里删了？")
    if ref.upper() == C.ENV_DEFAULT_API_KEY:
        _, default_base = ue.default_from_env()
        return api_key, default_base or ""
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
    from ..services import agent as agent_svc
    from ..services import upstream_env as ue

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
        _render(request, "_upstream_probe.html", {"probe": probe, "csrf": csrf_token})
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
    from ..services import upstream_env as ue

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
        return await _render_upstreams(
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

    from ..app import load_upstream

    await load_upstream(state)
    up = state.upstream.config
    if up is not None:
        await state.catalog.refresh(state.upstream.client(), up.api_key)
    state.runtimes.clear()

    return security_headers(RedirectResponse("/admin/upstreams", status_code=303))


# =============================================================================
# 配额
# =============================================================================

_WINDOW_LABELS = {"day": "每天", "month": "每月", "total": "累计"}
_SUBJECT_LABELS = {"user": "用户", "token": "密钥", "agent": "Agent"}


async def _quota_context(request: Request, error: str | None = None) -> dict[str, Any]:
    state = request.app.state.xc
    from ..services import agent as agent_svc
    from ..services import auth as auth_svc

    async with state.sessionmaker() as s:
        tokens = await auth_svc.list_tokens(s)
        agents = await agent_svc.list_active(s)
        admin = await ws.get_admin(s)

    # **一个下拉，选项自带类型。**
    #
    # 此前是两个：一个选类型（用户/密钥/Agent），一个选对象——而后者是三类平铺在
    # 一起的列表，两者不联动。于是"密钥 + 用户 admin"这种无效组合能被提交出去，
    # 存成一条指向不存在主体的规则，而页面上看不出哪里不对。
    #
    # 合成一个之后那一类错误不存在：选中的那一项自己就带着 type 和 id。
    #
    # 名字够用就不显 id。此前每一项后面都缀着 `（3）`，那是行 id——只有重名时才需要
    # 它来区分，平时纯属噪音，而且没人知道那个数字是什么。
    def _uniq(labels: list[str]) -> list[bool]:
        """哪些名字重复了——只有重复的才需要缀 id。"""
        seen: dict[str, int] = {}
        for label in labels:
            seen[label] = seen.get(label, 0) + 1
        return [seen[label] > 1 for label in labels]

    token_dup = _uniq([t.name for t in tokens])
    agent_dup = _uniq([a.slug for a in agents])

    # 分组在这里做好，不用 Jinja 的 ``groupby``——那个过滤器会按分组键排序，
    # 于是三层会被重排成字典序，而「账号 → 密钥 → Agent」的顺序本身是有含义的
    # （从粗到细）。
    subject_groups = [
        (
            "账号（全站合计）",
            [
                SimpleNamespace(
                    value="user:1",
                    label=admin.username if admin else "admin",
                    hint="这台星槎的全部用量",
                )
            ],
        ),
        (
            "密钥",
            [
                SimpleNamespace(
                    value=f"token:{t.id}",
                    label=f"{t.name}（{t.id}）" if dup else t.name,
                    hint="这把 sk-xc- 的用量",
                )
                for t, dup in zip(tokens, token_dup, strict=True)
            ],
        ),
        (
            "Agent",
            [
                SimpleNamespace(
                    value=f"agent:{a.agent_id}",
                    label=f"{a.slug}（{a.agent_id}）" if dup else a.slug,
                    hint="这个 Agent 的用量，不分是谁调的",
                )
                for a, dup in zip(agents, agent_dup, strict=True)
            ],
        ),
    ]
    subject_groups = [(g, items) for g, items in subject_groups if items]
    subjects = [item for _, items in subject_groups for item in items]

    label_of = {("user", 1): admin.username if admin else "admin"}
    label_of |= {("token", t.id): t.name for t in tokens}
    label_of |= {("agent", a.agent_id): a.slug for a in agents}

    rules = []
    if state.quota is not None:
        await state.quota.reload()
        for snap in state.quota.snapshot():
            st, sid = str(snap["subject_type"]), int(snap["subject_id"])  # type: ignore[arg-type]
            name = label_of.get((st, sid), f"#{sid}")
            limit_usd = snap["limit_usd"]
            limit_req = snap["limit_requests"]
            rules.append(
                SimpleNamespace(
                    subject_type=st,
                    subject_id=sid,
                    subject_label=f"{_SUBJECT_LABELS.get(st, st)} {name}",
                    window=snap["window"],
                    window_label=_WINDOW_LABELS.get(str(snap["window"]), snap["window"]),
                    spent_usd=_fmt_cost(str(snap["spent_usd"])),
                    limit_usd=limit_usd,
                    spent_requests=snap["spent_requests"],
                    limit_requests=limit_req,
                    period=snap["period"],
                    usd_over=bool(limit_usd)
                    and Decimal(str(snap["spent_usd"])) >= Decimal(str(limit_usd)),
                    req_over=bool(limit_req) and int(snap["spent_requests"]) >= int(limit_req),  # type: ignore[arg-type]
                )
            )

    return {
        "rules": rules,
        "subjects": subjects,
        "subject_groups": subject_groups,
        "error": error,
    }


async def _quota_error(request: Request, message: str) -> Response:
    """配额页就地回显错误。三处出错路径共用，省得各写一遍 csrf 的取放。"""
    csrf = await _ensure_csrf_cookie(request)
    resp = _render(
        request, "quota.html", {**await _quota_context(request, message), "csrf": csrf.value}
    )
    csrf.apply(resp)
    return resp


@router.get("/quota")
async def quota_page(request: Request) -> Response:
    await require_admin(request)
    csrf = await _ensure_csrf_cookie(request)
    resp = _render(request, "quota.html", {**await _quota_context(request), "csrf": csrf.value})
    csrf.apply(resp)
    return resp


@router.post("/quota/save")
async def quota_save(
    request: Request,
    subject: str = Form(...),
    window: str = Form(...),
    usd: str = Form(default=""),
    requests: str = Form(default=""),
    csrf_token: str = Form(default=""),
) -> Response:
    await guard_mutation(request, csrf_token)
    state = request.app.state.xc
    from ..services import quota as quota_svc

    # 表单里主体是**一个**字段，形如 `token:3`。拆开的动作放在最外层：
    # 里面那一层（quota_svc）的接口仍然是 ``(type, id)``，那是库里的形状，不该被 UI
    # 的表达方式带偏。
    #
    # 校验对着**页面自己渲染出来的那份选项**做，而不是只查一遍格式。格式对但对象
    # 不存在（`token:999`）会存下一条永远匹配不上的规则，表格里显示成 `#999`，
    # 而人看不出它为什么不生效。
    subject_type, _, raw_id = subject.partition(":")
    subject_id = int(raw_id) if raw_id.isdigit() else 0
    if subject_type not in quota_svc.SUBJECT_TYPES or not subject_id:
        return await _quota_error(request, f"主体不合法：{subject}")

    try:
        async with state.sessionmaker() as s:
            await quota_svc.upsert(
                s,
                subject_type=subject_type,
                subject_id=subject_id,
                window=window,
                limit_usd=Decimal(usd) if usd.strip() else None,
                limit_requests=int(requests) if requests.strip() else None,
            )
            await s.commit()
    except (quota_svc.InvalidQuota, ArithmeticError, ValueError) as e:
        return await _quota_error(request, str(e))

    # 规则改了要让内存里的计数器跟上，否则新规则要等下次重启才生效。
    if state.quota is not None:
        await state.quota.reload()
    return security_headers(RedirectResponse("/admin/quota", status_code=303))


@router.post("/quota/delete")
async def quota_delete(
    request: Request,
    subject_type: str = Form(...),
    subject_id: int = Form(...),
    window: str = Form(...),
    csrf_token: str = Form(default=""),
) -> Response:
    await guard_mutation(request, csrf_token)
    state = request.app.state.xc
    from ..services import quota as quota_svc

    async with state.sessionmaker() as s:
        await quota_svc.remove(s, subject_type=subject_type, subject_id=subject_id, window=window)
        await s.commit()
    if state.quota is not None:
        await state.quota.reload()
    return security_headers(RedirectResponse("/admin/quota", status_code=303))
