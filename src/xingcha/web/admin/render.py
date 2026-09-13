"""模板渲染与展示格式化。

后台的每个响应都要做同一串事：填公共上下文、种 CSRF cookie、加安全头。散着写过
一段时间，代价很具体——漏掉 CSRF 那一步的页面会静默 403，而且只有那一页的按钮
不好使。所以这里只留两个出口：

* :func:`render` —— 片段（htmx 换的那一小块），不碰 cookie；
* :func:`page` —— 整页，顺带把 CSRF cookie 种好。

**新加一个整页路由时用 :func:`page`。** 用 :func:`render` 也能出页面，
但那样就又回到了"每个人自己记得种 cookie"。

总览与调用记录两页至今仍走 :func:`render`。今天没有症状：两个 cookie 由登录一起
种下、同寿（见 security.set_session_cookies），所以 :func:`page` 那一步在实践中
总是"cookie 已经在"的空转。而 cookie 真丢了也不是它能救的——实测：删掉 ``xc_csrf``
再打开一个走 :func:`page` 的页面，它会现编一个新令牌，但那个令牌**不会写回**
``WebSession.csrf_hash``（只有登录时写一次），提交照样 403；走 :func:`render` 的
那两页则干脆渲染出空串。两条路都得重新登录。

所以这条规矩留着不是在挡一个正在发生的 bug，是为了**新页面别再依赖"别人先种过
cookie"**——那个前提哪天不成立，就又回到了只有某几个按钮不好使的状态。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ... import __version__
from ... import contract as C
from .assets import TEMPLATES_DIR, asset
from .security import CSRF_COOKIE, ensure_csrf_cookie, read_theme, security_headers

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["asset"] = asset


def render(request: Request, template: str, ctx: dict[str, Any]) -> HTMLResponse:
    """渲染一个模板，补齐公共上下文并加上安全头。

    整页请用 :func:`page`——它多做一步"确保 CSRF cookie 在"。
    """
    state = request.app.state.xc
    base = {
        "version": __version__,
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
    # 回落这一支是给走 render() 的整页用的（总览、调用记录）。它们不签发，只能读
    # cookie；cookie 不在就是空串，而那一页里其它自己传了 csrf 的表单照常工作——
    # 症状是"只有切主题不好使"。两个 cookie 现在同寿（见 security.Csrf.apply 的
    # max_age），所以这一支平时取得到值。
    merged["csrf_theme"] = merged.get("csrf") or request.cookies.get(CSRF_COOKIE, "")
    resp = templates.TemplateResponse(request, template, merged)
    return security_headers(resp)  # type: ignore[return-value]


async def page(request: Request, template: str, ctx: dict[str, Any]) -> HTMLResponse:
    """渲染一个**整页**：确保 CSRF cookie 在，把明文喂给模板，再种回响应。

    此前这三步是每个页面路由自己抄一遍的（抄了十一处）。抄漏一处不会报错，
    只会让那一页的表单在某些时机 403——见 :meth:`security.Csrf.apply` 里记的那次。
    """
    csrf = await ensure_csrf_cookie(request)
    resp = render(request, template, {"csrf": csrf.value, **ctx})
    csrf.apply(resp)
    return resp


def fmt_time(iso: str | None) -> str:
    """完整时间戳。**年和秒都要带上。**

    此前是 ``%m-%d %H:%M``。省掉年份，跨年之后"01-03"到底是哪一年只能靠猜；省掉秒，
    "有效期至 09-12 07:59" 无法回答"到底几点几分几秒过期"——而密钥详情页上这两个
    数字正是用来对时的。列表页多出来的六个字符换的是一个不需要再推断的时间。
    """
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return iso[:19]


#: 费用来源的人话解释。**实价与估价必须能一眼分开**——把两者显示成同一个样子，
#: 看账单的人会以为目录估价就是要付的钱，而两者能差几百倍。
COST_HINT = {
    C.CostSource.UPSTREAM.value: "上游报的实际费用",
    C.CostSource.CATALOG.value: "按模型目录价预估（非实际账单）",
    C.CostSource.GENAI_PRICES.value: "按 genai-prices 预估（非实际账单）",
    C.CostSource.UNKNOWN.value: "目录里没有这个模型的价格，无法定价",
}


def fmt_cost(value: str | None) -> str:
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
