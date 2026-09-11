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

import yaml
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


async def resolve(
    session: AsyncSession, slug: str, *, include_inactive: bool = False
) -> ResolvedAgent:
    """按 slug 取当前版本。找不到抛 :class:`ModelNotFound`。

    也查别名表：slug 发布后不可改名，改名的唯一出路是新建 Agent 并把旧 slug
    登记成别名，让老调用方继续能用。

    ``include_inactive`` 分开**运行时**与**管理面**两种读法，这不是可选的方便：

    * 运行时（``/v1``）必须只看启用的——停用的 slug 就该回 model_not_found，
      那正是"停用"的含义。
    * 管理面必须看得见停用的。否则停用之后编辑页 404：**改不了、看不了、连它
      为什么被停都查不到**，只剩列表页上一个开关。停用本该是可逆的，而一个进去
      就出不来的状态不叫可逆。实测踩到——把 Agent 全停之后整页的「编辑」全是死链。
    """
    active_only = [] if include_inactive else [Agent.is_active.is_(True)]
    row = (
        await session.execute(select(Agent).where(Agent.slug == slug, *active_only))
    ).scalar_one_or_none()

    if row is None:
        alias = (
            await session.execute(select(AgentAlias).where(AgentAlias.alias == slug))
        ).scalar_one_or_none()
        if alias is not None:
            row = (
                await session.execute(select(Agent).where(Agent.id == alias.agent_id, *active_only))
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


@dataclass(frozen=True, slots=True)
class SpecBundle:
    """一份待导入的 Agent 定义：解析并校验过的 ``AgentSpec`` + 输出 schema。"""

    slug: str
    spec: dict[str, Any]
    schema_text: str | None

    @property
    def model(self) -> str:
        return str(self.spec["model"])


def parse_bundle(
    yaml_text: str, *, slug: str | None = None, schema_text: str | None = None
) -> SpecBundle:
    """``agent.yaml`` 文本 → 可以交给 :func:`apply_bundle` 的一束。

    **导入的规矩集中在这一个函数里。** CLI 的 ``agent apply`` 与后台的「导入」
    都走它——两边各写一遍的话，其中一条路总会先漏掉某个字段，而症状是"导回来的
    Agent 少了点什么"：表单上看着正常，只有输出变了。
    """
    try:
        spec = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        raise AgentSpecInvalid(f"YAML 解析失败：{e}") from e
    if not isinstance(spec, dict):
        raise AgentSpecInvalid("文件内容不是一个 AgentSpec 映射。")

    # 入库前先过官方 AgentSpec 的 schema 校验。AgentSpec 是 extra='ignore'，
    # 直接构造会**静默吞掉拼错的字段**——那时候你以为配了、其实没配。
    builder.validate_spec(spec)

    model = spec.get("model")
    if not isinstance(model, str) or not model:
        raise AgentSpecInvalid("AgentSpec 里没有 model。")

    # schema 有两处可能的来源，都要认：
    #   · 单独给的 schema.json（export 的三文件形状）
    #   · spec 里内嵌的 output_schema（`agent show` 的输出、手写的 agent.yaml）
    # 只认前者的话，`agent show x > f.yaml && agent apply f.yaml` 会**静默把结构化
    # Agent 降成纯文本**——200 依旧，只是再也没有校验了，是最难发现的一种回归。
    if schema_text is None and isinstance(spec.get("output_schema"), dict):
        schema_text = json.dumps(spec["output_schema"], ensure_ascii=False)

    resolved = (slug or spec.get("name") or "").strip()
    if not resolved:
        raise AgentSpecInvalid("没有 slug：给一个，或让文件所在目录名当 slug。")
    return SpecBundle(slug=resolved, spec=spec, schema_text=schema_text)


async def apply_bundle(
    session: AsyncSession,
    bundle: SpecBundle,
    *,
    native_ok: bool,
    requested_tier: Tier | None = None,
    changelog: str = "",
    group_name: str | None = None,
) -> SaveResult:
    """把 :func:`parse_bundle` 的结果落库。

    ``native_ok`` 由调用方给：CLI 现拉一次目录，后台用进程里已有的那份。
    服务层不直接依赖 catalog，保持依赖方向单向。
    """
    spec = bundle.spec
    retries = spec.get("retries")
    return await save(
        session,
        slug=bundle.slug,
        name=str(spec.get("name") or bundle.slug),
        description=spec.get("description"),
        instructions=str(spec.get("instructions") or ""),
        model=bundle.model,
        schema_text=bundle.schema_text,
        requested_tier=requested_tier,
        capabilities=spec.get("capabilities"),
        # model_settings 与 prompting 也要带回来。导出物的 README 写着"改完还能
        # 导回来"，而漏掉一项的表现是：导回来的 Agent 少了 temperature、或者少了
        # 用户模板——表单上看着一切正常，只有输出变了。
        model_settings=spec.get("model_settings") or None,
        prompting=builder.prompting_from_spec(spec),
        retries=int(retries) if isinstance(retries, int | str) and str(retries).isdigit() else 2,
        native_ok=native_ok,
        group_name=group_name,
        changelog=changelog,
    )


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
    model_settings: dict[str, Any] | None = None,
    retries: int,
    native_ok: bool,
    prompting: builder.Prompting | None = None,
    group_name: str | None = None,
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
        model_settings=model_settings,
        retries=retries,
        prompting=prompting,
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
            group_name=(group_name or "").strip() or None,
            created_at=utcnow(),
        )
        session.add(row)
        await session.flush()
    else:
        row.name = name
        row.description = description
        # 分组是**表单里的一项**，所以每次保存都按表单来（包括清空回默认组）。
        # 只在非空时才写的话，"把它挪回默认组"这个动作就做不了。
        row.group_name = (group_name or "").strip() or None

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


#: 没分过组的 Agent 归到这个名字下**只在展示时**成立。
#:
#: 库里存的是 NULL，不是这四个字——"没分过组"和"被明确放进一个叫默认分组的组"
#: 是两件事，而把展示用的名字写进库会让这两件事再也分不开。
DEFAULT_GROUP = "默认分组"


async def list_groups(session: AsyncSession) -> list[str]:
    """已经用过的分组名，按名字排序。默认分组不在其中——它不是一个真实的分组。"""
    rows = (
        (
            await session.execute(
                select(Agent.group_name)
                .where(Agent.group_name.is_not(None))
                .distinct()
                .order_by(Agent.group_name)
            )
        )
        .scalars()
        .all()
    )
    return [r for r in rows if r]


#: 分组名的长度上限。与模板里的 ``maxlength`` 是同一个数。
GROUP_NAME_MAX = 40


async def declared_groups(session: AsyncSession, keyring: Any) -> list[str]:
    """在后台"新建分组"建过、但可能还没有成员的分组名。"""
    from . import setting as setting_svc

    raw = await setting_svc.get(session, keyring, C.SETTING_KEY_AGENT_GROUPS)
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except ValueError:
        # 手改坏了不该让整页 500——分组只是个展示分类。
        log.warning("agent.groups 不是合法 JSON，已忽略")
        return []
    return [str(i).strip() for i in items if isinstance(i, str) and str(i).strip()]


async def _write_declared(session: AsyncSession, keyring: Any, names: list[str]) -> None:
    from . import setting as setting_svc

    ordered = sorted(dict.fromkeys(names))
    await setting_svc.set_(
        session, keyring, C.SETTING_KEY_AGENT_GROUPS, json.dumps(ordered, ensure_ascii=False)
    )


async def declare_group(session: AsyncSession, keyring: Any, name: str) -> str:
    """登记一个分组名。返回规范化后的名字；已存在时是幂等的。

    校验放在这里而不是路由里：CLI 和后台走的是同一条路。
    """
    clean = " ".join(name.split())[:GROUP_NAME_MAX]
    if not clean:
        raise ValueError("分组名不能为空。")
    if clean == DEFAULT_GROUP:
        raise ValueError(f"「{DEFAULT_GROUP}」是没分组时的展示名，不能建成一个真的分组。")
    await _write_declared(session, keyring, [*await declared_groups(session, keyring), clean])
    return clean


async def all_groups(session: AsyncSession, keyring: Any) -> list[str]:
    """下拉框里该出现的全部分组：有成员的 + 登记过还空着的。"""
    return sorted({*await list_groups(session), *await declared_groups(session, keyring)})


async def rename_group(session: AsyncSession, keyring: Any, old: str, new: str) -> int:
    """把一个分组下的 Agent 全部挪到另一个名字下。返回挪了几个。

    新名字为空 = 挪回默认分组（写 NULL），并把这个名字从登记表里删掉——不然它会
    以一个空分组的身份留在下拉框里，而用户刚做的动作是"把它去掉"。
    """
    rows = (await session.execute(select(Agent).where(Agent.group_name == old))).scalars().all()
    target = " ".join(new.split())[:GROUP_NAME_MAX] or None
    for row in rows:
        row.group_name = target
    declared = [g for g in await declared_groups(session, keyring) if g != old]
    if target:
        declared.append(target)
    await _write_declared(session, keyring, declared)
    return len(rows)


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
