"""密钥页：签发、吊销与单把密钥的用量详情。"""

from __future__ import annotations

import logging
from types import SimpleNamespace

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select

from ...db.models import Token
from ...services import auth as auth_svc
from .render import fmt_time, page
from .runs import recent_runs, run_sources, run_stats
from .security import (
    current_session,
    guard_mutation,
    require_admin,
    security_headers,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", include_in_schema=False)


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
            "last_used_display": fmt_time(t.last_used_at),
            "created_display": fmt_time(t.created_at),
        }
        for t in rows
    ]
    return await page(
        request,
        "keys.html",
        {
            "tokens": tokens,
            "issued": {"plaintext": issued, "name": name} if issued else None,
        },
    )


@router.get("/keys/{kid}")
async def key_detail(kid: str, request: Request) -> Response:
    """一把密钥的调用详情。

    **key 泄漏时第一个要回答的问题是"它现在被谁在用"**，而密钥列表答不了：那里
    只有"最后使用"一个时间点。这一页给的是来源、频次、失败构成和最近的调用行。
    """
    await require_admin(request)
    state = request.app.state.xc

    async with state.sessionmaker() as s:
        token = (await s.execute(select(Token).where(Token.kid == kid))).scalar_one_or_none()
        if token is None:
            # 后台页的 404 就该是重定向回列表，不是一个错误 JSON：这里的读者是人。
            return security_headers(RedirectResponse("/admin/keys", status_code=303))
        stats = await run_stats(s, token_id=token.id)
        sources = await run_sources(s, token_id=token.id)
        runs = await recent_runs(s, limit=100, token_id=token.id)

    return await page(
        request,
        "key_detail.html",
        {
            "token": SimpleNamespace(
                name=token.name,
                kid=token.kid,
                display_prefix=token.display_prefix,
                is_active=token.is_active,
                expired=auth_svc.is_expired(token),
                created=fmt_time(token.created_at),
                last_used=fmt_time(token.last_used_at),
                expires=fmt_time(token.expires_at) if token.expires_at else "永不过期",
            ),
            "stats": stats,
            "sources": sources,
            "runs": runs,
        },
    )


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
