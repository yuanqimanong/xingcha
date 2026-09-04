"""Agent 的增删改查与版本管理。

**版本不可变。** 每次保存产生一个新的 ``agent_version`` 行，``agent.current_version_id``
指向当前生效的那个。回滚就是把指针挪回去——不是把旧内容写回来。

这样做的直接收益是运行时缓存不需要失效逻辑：Agent 实例按 ``(agent_id, version)``
缓存，编辑产生新版本号，旧条目自然不再被命中。缓存失效是这类系统最容易出错的地方，
用不可变版本把它绕过去。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import contract as C
from ..contract import ModelRefInvalid, Tier, validate_slug
from ..core import builder
from ..core.guarantee import resolve_tier
from ..core.schema_guard import SchemaRejected, validate_schema
from ..db.models import Agent, AgentAlias, AgentVersion, utcnow
from ..errors import AgentSpecInvalid, ModelNotFound

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ResolvedAgent:
    """一个 Agent 及其当前版本。运行时需要的一切都在这里。"""

    agent_id: int
    slug: str
    name: str
    description: str | None
    version: int
    version_id: int
    spec_json: str
    tier: Tier
    out_schema: str | None
    created_at: str

    @property
    def is_structured(self) -> bool:
        return self.out_schema is not None


class SlugTaken(ValueError):
    def __init__(self, slug: str) -> None:
        super().__init__(
            f"标识 {slug!r} 已被占用。标识是全局唯一的，而且**发布后不能改名**——"
            "调用方的代码里写着它。换一个名字，或者停用那个 Agent。"
        )


async def resolve(session: AsyncSession, slug: str) -> ResolvedAgent:
    """按 slug 取当前版本。找不到抛 :class:`ModelNotFound`。

    也查别名表：slug 发布后不可改名，改名的唯一出路是新建 Agent 并把旧 slug
    登记成别名，让老调用方继续能用。
    """
    row = (
        await session.execute(select(Agent).where(Agent.slug == slug, Agent.is_active.is_(True)))
    ).scalar_one_or_none()

    if row is None:
        alias = (
            await session.execute(select(AgentAlias).where(AgentAlias.alias == slug))
        ).scalar_one_or_none()
        if alias is not None:
            row = (
                await session.execute(
                    select(Agent).where(Agent.id == alias.agent_id, Agent.is_active.is_(True))
                )
            ).scalar_one_or_none()

    if row is None or row.current_version_id is None:
        raise ModelNotFound(slug)

    ver = await session.get(AgentVersion, row.current_version_id)
    if ver is None:  # pragma: no cover - 外键保证不该发生
        raise ModelNotFound(slug)

    return ResolvedAgent(
        agent_id=row.id,
        slug=row.slug,
        name=row.name,
        description=row.description,
        version=ver.version,
        version_id=ver.id,
        spec_json=ver.spec_json,
        tier=Tier(ver.tier),
        out_schema=ver.out_schema,
        created_at=row.created_at,
    )


async def list_active(session: AsyncSession) -> list[ResolvedAgent]:
    """列出启用的 Agent，按创建时间升序。

    顺序进了契约：``GET /v1/models`` 里 Agent 行必须稳定排在前面且顺序固定，
    因为部分客户端取 ``data[0]`` 当默认模型。
    """
    rows = (
        (
            await session.execute(
                select(Agent).where(Agent.is_active.is_(True)).order_by(Agent.created_at)
            )
        )
        .scalars()
        .all()
    )

    out: list[ResolvedAgent] = []
    for row in rows:
        if row.current_version_id is None:
            continue
        ver = await session.get(AgentVersion, row.current_version_id)
        if ver is None:
            continue
        out.append(
            ResolvedAgent(
                agent_id=row.id,
                slug=row.slug,
                name=row.name,
                description=row.description,
                version=ver.version,
                version_id=ver.id,
                spec_json=ver.spec_json,
                tier=Tier(ver.tier),
                out_schema=ver.out_schema,
                created_at=row.created_at,
            )
        )
    return out


async def list_all(session: AsyncSession) -> list[tuple[Agent, AgentVersion | None]]:
    """管理面用：含停用的。"""
    rows = (await session.execute(select(Agent).order_by(Agent.created_at.desc()))).scalars().all()
    out = []
    for row in rows:
        ver = (
            await session.get(AgentVersion, row.current_version_id)
            if row.current_version_id
            else None
        )
        out.append((row, ver))
    return out


@dataclass(frozen=True, slots=True)
class SaveResult:
    agent_id: int
    slug: str
    version: int
    tier: Tier
    #: 判档被降级时的说明，供表单回显。
    tier_note: str = ""


async def save(
    session: AsyncSession,
    *,
    slug: str,
    name: str,
    description: str | None,
    instructions: str,
    model: str,
    schema_text: str | None,
    requested_tier: Tier | None,
    capabilities: list[str] | None,
    retries: int,
    native_ok: bool,
    changelog: str = "",
    user_id: int = 1,
) -> SaveResult:
    """新建或更新一个 Agent。总是产生一个新版本。

    ``native_ok`` 由调用方从模型目录查出来（``structured_outputs``）——服务层不直接
    依赖 catalog，保持依赖方向单向。
    """
    try:
        validate_slug(slug)
    except ModelRefInvalid as e:
        raise AgentSpecInvalid(str(e)) from e

    # --- schema：校验并拿回内联展开后的版本 ---
    inlined: dict[str, Any] | None = None
    if schema_text and schema_text.strip():
        try:
            inlined = validate_schema(schema_text)
        except SchemaRejected as e:
            raise AgentSpecInvalid(str(e)) from e

    choice = resolve_tier(requested_tier, has_schema=inlined is not None, native_ok=native_ok)

    spec = builder.spec_from_form(
        name=name,
        description=description,
        instructions=instructions,
        model=model,
        capabilities=capabilities,
        retries=retries,
    )
    # schema 也写进 spec：导出物靠它工作（from_file 没有 output_type 注入点），
    # 而运行时走的是 builder 显式传的 output_type，两者不冲突。
    if inlined is not None:
        spec["output_schema"] = inlined
    spec = builder.validate_spec(spec)

    # --- 落库 ---
    row = (await session.execute(select(Agent).where(Agent.slug == slug))).scalar_one_or_none()
    if row is None:
        row = Agent(
            slug=slug,
            name=name,
            description=description,
            is_active=True,
            user_id=user_id,
            created_at=utcnow(),
        )
        session.add(row)
        await session.flush()
    else:
        row.name = name
        row.description = description

    next_version = (
        await session.execute(
            select(func.coalesce(func.max(AgentVersion.version), 0)).where(
                AgentVersion.agent_id == row.id
            )
        )
    ).scalar_one() + 1

    ver = AgentVersion(
        agent_id=row.id,
        version=next_version,
        spec_json=json.dumps(spec, ensure_ascii=False),
        tier=choice.tier.value,
        # 落库的是**内联展开后**的 schema：校验器与模型必须看同一份约束，
        # 否则一个通过另一个不通过，而且没人看得出为什么。
        out_schema=json.dumps(inlined, ensure_ascii=False) if inlined else None,
        changelog=changelog or None,
        user_id=user_id,
        created_at=utcnow(),
    )
    session.add(ver)
    await session.flush()
    row.current_version_id = ver.id

    return SaveResult(
        agent_id=row.id,
        slug=slug,
        version=next_version,
        tier=choice.tier,
        tier_note=choice.reason,
    )


async def versions(session: AsyncSession, agent_id: int) -> list[AgentVersion]:
    return list(
        (
            await session.execute(
                select(AgentVersion)
                .where(AgentVersion.agent_id == agent_id)
                .order_by(AgentVersion.version.desc())
            )
        )
        .scalars()
        .all()
    )


async def rollback(session: AsyncSession, agent_id: int, version: int) -> bool:
    """回滚 = 把 ``current_version_id`` 指回去。不改写任何历史版本。"""
    ver = (
        await session.execute(
            select(AgentVersion).where(
                AgentVersion.agent_id == agent_id, AgentVersion.version == version
            )
        )
    ).scalar_one_or_none()
    if ver is None:
        return False
    row = await session.get(Agent, agent_id)
    if row is None:
        return False
    row.current_version_id = ver.id
    return True


async def set_active(session: AsyncSession, agent_id: int, active: bool) -> bool:
    row = await session.get(Agent, agent_id)
    if row is None:
        return False
    row.is_active = active
    return True


async def slug_available(session: AsyncSession, slug: str) -> bool:
    taken = (await session.execute(select(Agent.id).where(Agent.slug == slug))).scalar_one_or_none()
    if taken is not None:
        return False
    aliased = (
        await session.execute(select(AgentAlias.id).where(AgentAlias.alias == slug))
    ).scalar_one_or_none()
    return aliased is None


def agent_row_for_models_api(a: ResolvedAgent) -> dict[str, Any]:
    """``GET /v1/models`` 里的一行。形状进了契约，不要随手加顶层键。"""
    from datetime import datetime

    try:
        created = int(datetime.fromisoformat(a.created_at).timestamp())
    except ValueError:
        created = 0
    return {
        "id": a.slug,
        "object": "model",
        "created": created,
        "owned_by": C.OWNED_BY_XINGCHA,
        C.EXT_KEY: {
            "v": C.EXT_SHAPE_VERSION,
            "kind": "agent",
            "tier": a.tier.value,
            "description": a.description,
            "structured": a.is_structured,
        },
    }
