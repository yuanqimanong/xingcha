"""后台的准入：会话、CSRF、同源与安全响应头。

**每一个改状态的请求都必须经过 :func:`guard_mutation`。** 后台暴露在公网上，
而它里面有一个能改写上游 base_url 的表单：一次成功的 CSRF 就等于把付费 key 送到
攻击者的服务器。所以三层叠加：SameSite=Strict cookie、double-submit token、
Origin/Sec-Fetch-Site 校验。

这些东西单独成一个模块，是为了让"哪些请求受保护"能被一眼数清——散在各页里的时候，
新加一个 POST 忘记加守卫不会有任何提示。
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from fastapi import Request
from fastapi.responses import Response

from ... import contract as C
from ...services import websession as ws

#: 承载 CSRF 明文的 cookie。库里存的是哈希，所以明文只能从这里回到表单。
CSRF_COOKIE = "xc_csrf"

#: cookie 的作用域。限死在后台路径下，``/v1`` 的请求不会白带上它们。
COOKIE_PATH = "/admin"


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


def set_session_cookies(resp: Response, *, token: str, csrf: str, request: Request) -> None:
    """登录成功后一次性种下会话与 CSRF 两个 cookie。

    两者**必须同寿**，所以只有这一个出口。分开设置过一次，代价见 :meth:`Csrf.apply`。
    """
    ttl = request.app.state.xc.settings.session_ttl_hours * 3600
    secure = cookie_secure(request)
    resp.set_cookie(
        ws.SESSION_COOKIE,
        token,
        httponly=True,
        samesite="strict",
        secure=secure,
        path=COOKIE_PATH,
        max_age=ttl,
    )
    resp.set_cookie(
        CSRF_COOKIE,
        csrf,
        httponly=False,  # 表单要读它
        samesite="strict",
        secure=secure,
        path=COOKIE_PATH,
        max_age=ttl,
    )


def clear_session_cookies(resp: Response) -> None:
    """登出。两个 cookie 一起清，留一个都会让下一次登录拿到过期的令牌。"""
    resp.delete_cookie(ws.SESSION_COOKIE, path=COOKIE_PATH)
    resp.delete_cookie(CSRF_COOKIE, path=COOKIE_PATH)


@dataclass(frozen=True, slots=True)
class Csrf:
    """这一次渲染要用的 CSRF 明文，以及"要不要顺手把它种进 cookie"。

    分成"取值"与"落 cookie"两步，是因为值要先进模板上下文、cookie 要设在最终的
    ``Response`` 上，而中间隔着一次渲染。
    """

    value: str
    fresh: bool
    secure: bool = True
    max_age: int = 0

    def apply(self, resp: Response) -> None:
        if not self.fresh:
            return
        resp.set_cookie(
            CSRF_COOKIE,
            self.value,
            httponly=False,  # 表单要读它；它本身不是凭证，只是"你能读到本站页面"的证明
            samesite="strict",
            secure=self.secure,
            path=COOKIE_PATH,
            # **必须和会话同寿**。此前没有 max_age，也就是浏览器会话级：
            # 关掉浏览器再打开，xc_session 还在（它有 7 天），xc_csrf 已经没了。
            # 于是任何"从 cookie 里取令牌"的表单都会 403，而同一页里自己签发
            # 令牌的表单照常工作——症状是"只有某几个按钮不好使"。
            max_age=self.max_age or None,
        )


async def ensure_csrf_cookie(request: Request) -> Csrf:
    existing = request.cookies.get(CSRF_COOKIE)
    if existing:
        return Csrf(existing, fresh=False)
    return Csrf(
        secrets.token_urlsafe(32),
        fresh=True,
        secure=cookie_secure(request),
        max_age=request.app.state.xc.settings.session_ttl_hours * 3600,
    )
