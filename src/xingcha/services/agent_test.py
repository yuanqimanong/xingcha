"""后台「试运行」的记录。

按 ``slug`` 只保留**最近 3 条**，写入时顺手删旧的。

不设保留期而是设条数上限，是因为这张表天然有界：一个 Agent 最多 3 条、Agent 是
个位数到几十。而它存着完整消息链——提示词原文与模型输出都在里面——无界增长就是
一个越来越大的、装着对话内容的表。这一点与 ``run`` 相反：那张表从不存内容，所以
它的清理是按时间（``xingcha db prune``）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import AgentTestRun, utcnow

log = logging.getLogger(__name__)

#: 每个 slug 留几条。3 是用户定的：够看出"改了提示词之后有没有变好"，又不至于
#: 让这张表变成一份对话归档。
KEEP_PER_SLUG = 3

#: 单条消息链的字符上限。链路里可能有一整篇被抽取的合同，不封顶就等于把请求体
#: 大小（8 MB）搬进数据库。截断优于拒绝：面板的用途是"扫一眼哪里不对"。
CHAIN_MAX_CHARS = 60_000


async def record(
    session: AsyncSession,
    *,
    slug: str,
    model: str,
    tier: str | None,
    ok: bool,
    prompt: str,
    output: str | None = None,
    error: str | None = None,
    chain: list[Any] | None = None,
    elapsed_ms: int | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    requests: int = 0,
    violations: int = 0,
    retries: int = 0,
    cost_usd: str | None = None,
    cost_source: str | None = None,
) -> None:
    """记一次试运行，并把这个 slug 下多出来的旧记录删掉。"""
    payload = json.dumps(chain or [], ensure_ascii=False, default=str)
    if len(payload) > CHAIN_MAX_CHARS:
        payload = json.dumps(
            [{"role": "req", "label": "（链路过长，已截断）", "text": payload[:CHAIN_MAX_CHARS]}],
            ensure_ascii=False,
        )

    session.add(
        AgentTestRun(
            slug=slug or "",
            model=model,
            tier=tier,
            ok=ok,
            input=prompt,
            output=output,
            error=error,
            chain_json=payload,
            elapsed_ms=elapsed_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            requests=requests,
            violations=violations,
            retries=retries,
            cost_usd=cost_usd,
            cost_source=cost_source,
            created_at=utcnow(),
        )
    )
    await session.flush()
    await _trim(session, slug or "")


async def _trim(session: AsyncSession, slug: str) -> None:
    """只留最近 KEEP_PER_SLUG 条。

    按 **id 倒序**取要保留的那几条，不按 created_at：同一秒内跑两次时时间戳可能
    相同，那时候 created_at 排序是不确定的，会随机删掉刚写的那条。
    """
    keep = (
        (
            await session.execute(
                select(AgentTestRun.id)
                .where(AgentTestRun.slug == slug)
                .order_by(AgentTestRun.id.desc())
                .limit(KEEP_PER_SLUG)
            )
        )
        .scalars()
        .all()
    )
    if len(keep) < KEEP_PER_SLUG:
        return
    await session.execute(
        delete(AgentTestRun).where(AgentTestRun.slug == slug, AgentTestRun.id.notin_(list(keep)))
    )


async def recent(session: AsyncSession, slug: str) -> list[AgentTestRun]:
    """这个 slug 最近的几次试运行，新的在前。"""
    return list(
        (
            await session.execute(
                select(AgentTestRun)
                .where(AgentTestRun.slug == (slug or ""))
                .order_by(AgentTestRun.id.desc())
                .limit(KEEP_PER_SLUG)
            )
        )
        .scalars()
        .all()
    )


def chain_of(row: AgentTestRun) -> list[Any]:
    """把存下来的链路读回成模板认识的形状。解析不了就当空——面板不该因此 500。"""
    from types import SimpleNamespace

    try:
        items = json.loads(row.chain_json or "[]")
    except ValueError:
        return []
    return [
        SimpleNamespace(
            role=str(i.get("role", "req")),
            label=str(i.get("label", "")),
            text=str(i.get("text", "")),
            meta=str(i.get("meta", "")),
        )
        for i in items
        if isinstance(i, dict)
    ]
