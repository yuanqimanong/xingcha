"""配额页。"""

from __future__ import annotations

import logging
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from ...services import agent as agent_svc
from ...services import auth as auth_svc
from ...services import quota as quota_svc
from ...services import websession as ws
from .render import fmt_cost, page
from .security import (
    guard_mutation,
    require_admin,
    security_headers,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", include_in_schema=False)


_WINDOW_LABELS = {"day": "每天", "month": "每月", "total": "累计"}


_SUBJECT_LABELS = {"user": "用户", "token": "密钥", "agent": "Agent"}


async def _quota_context(request: Request, error: str | None = None) -> dict[str, Any]:
    state = request.app.state.xc

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
                    spent_usd=fmt_cost(str(snap["spent_usd"])),
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
    """配额页就地回显错误。填了一半的表单不该因为一条报错被清空。"""
    return await page(request, "quota.html", await _quota_context(request, message))


@router.get("/quota")
async def quota_page(request: Request) -> Response:
    await require_admin(request)
    return await page(request, "quota.html", await _quota_context(request))


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

    async with state.sessionmaker() as s:
        await quota_svc.remove(s, subject_type=subject_type, subject_id=subject_id, window=window)
        await s.commit()
    if state.quota is not None:
        await state.quota.reload()
    return security_headers(RedirectResponse("/admin/quota", status_code=303))
