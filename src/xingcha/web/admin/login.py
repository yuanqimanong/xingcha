"""登录、登出与主题切换。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from ... import contract as C
from ...services import websession as ws
from .render import page, render
from .security import (
    Denied,
    check_origin,
    clear_session_cookies,
    cookie_secure,
    current_session,
    guard_mutation,
    security_headers,
    set_session_cookies,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", include_in_schema=False)


_throttle = ws.LoginThrottle()


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

    return await page(
        request,
        "login.html",
        {
            "setup": setup,
            "env_managed": env_managed,
            "action": "/admin/login",
            "error": None,
            # **登录页永远是暗色**，不跟随主题。银河是夜景——"亮色银河"是个矛盾：
            # 白底上的星点不像星，像页面没渲染干净。与其做一套注定难看的亮色，
            # 不如让这一页只有一种样子。后台各页照旧跟随用户的选择。
            "theme": "dark",
        },
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
    set_session_cookies(resp, token=new.token, csrf=new.csrf, request=request)
    return security_headers(resp)


def _login_error(request: Request, message: str, *, setup: bool = False) -> Response:
    # `env_managed` 也要传：漏了的话失败重渲染时"密码由环境变量托管"那行提示会消失，
    # 于是用户在最需要这条信息的时刻（刚输错）看不到它。
    env_managed = ws.env_password_in_effect(None, request.app.state.xc.settings.admin_password)
    return render(
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
    clear_session_cookies(resp)
    return security_headers(resp)
