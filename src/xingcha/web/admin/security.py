"""后台的准入：会话、CSRF、同源与安全响应头。

每一个改状态的请求都必须经过 :func:`guard_mutation`。后台里有一个能改写上游 base_url
的表单，一次成功的 CSRF 就等于把付费 key 送到攻击者的服务器，所以三层叠加：
SameSite=Strict cookie、double-submit token、Origin/Sec-Fetch-Site 校验。

单独成一个模块，是为了让"哪些请求受保护"能被一眼数清——散在各页里的话，新加一个 POST
忘记加守卫不会有任何提示。
"""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from dataclasses import dataclass

from fastapi import Request
from fastapi.responses import Response

from ... import contract as C
from ...services import websession as ws

#: 承载 CSRF 明文的 cookie。库里存的是哈希，所以明文只能从这里回到表单。
CSRF_COOKIE = "xc_csrf"

#: cookie 的作用域。限死在后台路径下，``/v1`` 的请求不会白带上它们。
COOKIE_PATH = "/admin"

#: 允许把后台嵌进 iframe、并被当作同站放行的来源。由 :func:`configure` 在装配后台时按
#: ``XINGCHA_ADMIN_EMBED_ORIGINS`` 装入，之后不再变。
#:
#: 放模块级而不是 ``app.state``：它启动即定、永不失效，没有"该在哪儿让它过期"的问题；
#: 而 :func:`security_headers` 有二十来处调用点，为一个常量把 ``Request`` 穿进每一处
#: 只会让"哪些响应带了安全头"更难数清。
_EMBED_ORIGINS: tuple[str, ...] = ()

_CSP_TEMPLATE = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; frame-ancestors {frame_ancestors}; "
    "base-uri 'none'; form-action 'self'"
)

_CSP = _CSP_TEMPLATE.format(frame_ancestors="'none'")


def configure(embed_origins: Sequence[str] = ()) -> None:
    """装配后台时调用一次，把"谁可以嵌我"定下来。默认（没配）是
    ``frame-ancestors 'none'`` + ``X-Frame-Options: DENY`` + 同源校验只认自己。
    """
    global _EMBED_ORIGINS, _CSP
    _EMBED_ORIGINS = tuple(embed_origins)
    _CSP = _CSP_TEMPLATE.format(
        frame_ancestors=" ".join(_EMBED_ORIGINS) if _EMBED_ORIGINS else "'none'"
    )


class Denied(Exception):
    """后台层面的拒绝。不走 /v1 的错误契约——那是给 SDK 用的，这里是给人看的。"""

    def __init__(self, message: str, status: int = 403) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def security_headers(resp: Response) -> Response:
    """每个后台响应都带上。

    ``frame-ancestors`` 挡点击劫持（把后台套进透明 iframe 诱导管理员点一下，效果等同
    CSRF）。默认 ``'none'``，配了
    :attr:`~xingcha.config.Settings.admin_embed_origins` 就换成那份名单。

    配了名单时不再发 ``X-Frame-Options``：它只有 DENY / SAMEORIGIN 两档，表达不了"只允
    许某个源"，留着 DENY 会把 CSP 刚放行的那个源又挡回去，而且浏览器优先采信它。
    """
    resp.headers["Content-Security-Policy"] = _CSP
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    if not _EMBED_ORIGINS:
        resp.headers["X-Frame-Options"] = "DENY"
    return resp


def check_origin(request: Request) -> None:
    """校验请求确实来自本站。

    ``Sec-Fetch-Site`` 现代浏览器一定会带且不可被脚本伪造，``Origin`` 作为老浏览器的
    回退。两个都没有时放行——非浏览器客户端（curl）本来就不受 CSRF 影响。

    显式配进 :data:`_EMBED_ORIGINS` 的来源两条分支都绕过。嵌进别的门户时这一步是必需
    的：门户用同源反代挂后台时，浏览器发来的 ``Origin`` 是门户的源，与我们看到的
    ``Host`` 永远不符，登录一提交就被拦下，而拦下的原因和"真有人跨站打你"长得一样。
    放行的只是这一层，SameSite=Strict cookie 与 double-submit token 一条都没动。
    """
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") in _EMBED_ORIGINS:
        return

    site = request.headers.get("sec-fetch-site")
    if site is not None:
        if site not in {"same-origin", "same-site", "none"}:
            raise Denied(f"跨站请求被拒绝（Sec-Fetch-Site: {site}）")
        return

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
    """每一个改状态的请求都要过这里。

    三层叠加不是冗余：SameSite 挡不住老浏览器，double-submit 挡不住能读到页面的同站
    脚本注入，Origin 校验挡不住不发这些头的客户端。
    """
    check_origin(request)
    row = await require_admin(request)
    header_token = request.headers.get(ws.CSRF_HEADER)
    if not (ws.csrf_matches(row, csrf_token) or ws.csrf_matches(row, header_token)):
        raise Denied("CSRF 校验失败。请刷新页面后重试。")


def read_theme(request: Request) -> str:
    """当前主题，用于 ``<html data-theme="...">``。返回 ``""``（跟随系统）、``"light"``
    或 ``"dark"``；cookie 里是别的值就当没设——那一格用户可写，不能直接塞进 HTML 属性。
    """
    value = request.cookies.get(C.THEME_COOKIE, "system")
    if value not in C.THEMES or value == "system":
        return ""
    return value


def cookie_secure(request: Request) -> bool:
    """会话与 CSRF cookie 要不要带 ``Secure``。跟随请求自身的协议，不写死。

    写死 ``True``：纯 HTTP 部署下浏览器直接丢掉 cookie，症状是"密码输对了却一直跳回
    登录页"而服务端日志显示登录成功（``localhost`` 例外，所以本机开发看不出问题，换成
    局域网 IP 才炸）。写死 ``False``：HTTPS 部署下攻击者能把受害者引到同域的 http 链接，
    让浏览器把凭证明文发出来。所以只有问这次请求本身一个答案。

    反代后面要额外一步：uvicorn 默认不读 ``X-Forwarded-Proto``（读了就等于信任任何人
    伪造的那个头），所以要显式开 ``proxy_headers`` 并把 ``forwarded_allow_ips`` 限定到
    反代的地址。
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
    """这一次渲染要用的 CSRF 明文，以及"要不要顺手把它种进 cookie"。分两步是因为值要
    先进模板上下文、cookie 要设在最终的 ``Response`` 上，中间隔着一次渲染。
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
