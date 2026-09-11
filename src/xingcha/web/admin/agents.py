"""Agent 页：列表、分组、增删改与导出。"""

from __future__ import annotations

import io
import json
import logging
import tempfile
import zipfile
from itertools import zip_longest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select

from ... import contract as C
from ...core import builder, exporter
from ...core.guarantee import TIER_INFO
from ...db.models import Agent as AgentRow
from ...errors import XingchaError
from ...services import agent as agent_svc
from ...services import agent_test as test_svc
from .agent_view import (
    active_upstream_label,
    empty_form,
    form_shell,
    lint_ctx,
    prompting_from_form,
    put_agents_flash,
    settings_view,
    take_agents_flash,
    take_saved,
    test_history_row,
)
from .render import page
from .runs import NO_RUNS, agent_summaries
from .security import (
    Denied,
    current_session,
    guard_mutation,
    require_admin,
    security_headers,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", include_in_schema=False)

#: 导入文件的大小上限。导出物是四个小文本文件，几十 KB 顶天；给到 1 MB 已经很松。
#: 有上限是因为这个端点会把整个文件读进内存再解压。
MAX_IMPORT_BYTES = 1024 * 1024


def _read_bundle(raw: bytes, filename: str, slug: str | None) -> tuple[str, str | None, str | None]:
    """从上传的文件里取出 ``agent.yaml`` 与 ``schema.json``。

    zip 里的目录名就是 slug（导出物的形状是 ``<slug>/agent.yaml``），所以整包传回来
    不用再填一次标识。**只按名字取这两个文件，不解压任何别的东西**——zip 里还有
    ``run.py``，而"把上传的压缩包整个展开到磁盘"是一类经典漏洞。
    """
    if not filename.lower().endswith(".zip"):
        return raw.decode("utf-8", "replace"), None, slug

    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as e:
        raise Denied("这不是一个能打开的 zip。") from e

    names = zf.namelist()
    spec_name = next((n for n in names if n.endswith("agent.yaml")), None)
    if spec_name is None:
        raise Denied("zip 里没有 agent.yaml —— 这不像是「导出」给出的那个包。")
    schema_name = next((n for n in names if n.endswith("schema.json")), None)

    inner = spec_name.rsplit("/", 2)
    if slug is None and len(inner) >= 2 and inner[-2]:
        slug = inner[-2]
    return (
        zf.read(spec_name).decode("utf-8"),
        zf.read(schema_name).decode("utf-8") if schema_name else None,
        slug,
    )


async def _import_error(request: Request, message: str) -> Response:
    """导入失败回到列表页并把原因说出来。

    不跳错误页：导入多半是复制粘贴出的问题（YAML 缩进、少了 model），用户需要的是
    回到能重试的地方，而不是一个只能后退的死胡同。
    """
    session = await current_session(request)
    put_agents_flash(request, session, "danger", f"导入失败：{message}")
    return security_headers(RedirectResponse("/admin/agents", status_code=303))


@router.get("/agents")
async def agents_page(request: Request) -> Response:
    session = await require_admin(request)
    state = request.app.state.xc

    async with state.sessionmaker() as s:
        pairs = await agent_svc.list_all(s)
        stats = await agent_summaries(s)
        declared = await agent_svc.declared_groups(s, state.keyring)
        trials = await test_svc.recent_many(s, [row.slug for row, _ in pairs])

    # 换上游之后有些 Agent 写的模型可能已经不存在了（切换页会先列出来让你确认，
    # 但确认过之后这件事只剩在这一页上看得见）。
    #
    # **只在目录真的拉到东西时才判**：目录为空有两种原因——上游没有 /models 端点，
    # 或者这次没拉到。那时把每个 Agent 都标成失效是在撒谎，而且撒得很响。
    catalog = state.catalog.all()
    catalog_known = bool(catalog)

    groups: dict[str, list[Any]] = {}
    stale_total = 0
    for row, ver in pairs:
        spec = json.loads(ver.spec_json) if ver else {}
        tier = ver.tier if ver else "—"
        st = stats.get(row.id) or NO_RUNS
        prompting = builder.prompting_from_spec(spec)
        model_id = spec.get("model") or ""
        model_stale = bool(catalog_known and model_id and state.catalog.get(model_id) is None)
        stale_total += 1 if model_stale else 0
        groups.setdefault(row.group_name or agent_svc.DEFAULT_GROUP, []).append(
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
                examples=len(prompting.examples),
                templated=bool(prompting.user_template),
                total=st.total,
                ok_rate=st.ok_rate,
                failed=st.failed,
                cost=st.cost,
                # 卡片上光写一个费用数字，而其中大半调用其实查不到价，那就是在
                # 撒谎——这正是"可定价率"这个指标要挡住的事，卡片上也得挡住。
                unpriced=st.unpriced,
                last=st.last,
                # 卡片上的「测试」弹窗要能直接看到前几次跑的是什么，不用先跳去编辑页。
                trials=[test_history_row(r) for r in trials.get(row.slug, [])],
                model_stale=model_stale,
            )
        )

    # 登记过、还没有成员的分组也要出现——不然"新建分组"点完页面毫无变化。
    for name in declared:
        groups.setdefault(name, [])

    # 默认分组排最后：它是"还没归类"的那堆，不该占着第一屏。
    ordered = sorted(groups.items(), key=lambda kv: (kv[0] == agent_svc.DEFAULT_GROUP, kv[0]))
    return await page(
        request,
        "agents.html",
        {
            "groups": ordered,
            "default_group": agent_svc.DEFAULT_GROUP,
            "agent_count": len(pairs),
            "stale_total": stale_total,
            "catalog_known": catalog_known,
            "upstream_label": active_upstream_label(state),
            "group_names": sorted(n for n in groups if n != agent_svc.DEFAULT_GROUP),
            "group_name_max": agent_svc.GROUP_NAME_MAX,
            "flash": take_agents_flash(request, session),
        },
    )


@router.post("/agents/group/rename")
async def rename_agent_group(
    request: Request,
    old: str = Form(...),
    new: str = Form(default=""),
    csrf_token: str = Form(default=""),
) -> Response:
    """给一个分组改名，或（``new`` 为空时）把它整组挪回默认分组。

    没有"新建空分组"这个动作：分组不是一张表，就是 Agent 上的一个字符串，而一个
    没有成员的分组没有任何意义。要新建就在某个 Agent 的表单里写一个新名字。
    """
    await guard_mutation(request, csrf_token)
    state = request.app.state.xc

    async with state.sessionmaker() as s:
        await agent_svc.rename_group(s, state.keyring, old, new)
        await s.commit()
    return security_headers(RedirectResponse("/admin/agents", status_code=303))


@router.post("/agents/group/create")
async def create_agent_group(
    request: Request,
    name: str = Form(default=""),
    csrf_token: str = Form(default=""),
) -> Response:
    """登记一个空分组。

    "先建分组、再往里放 Agent" 是人的自然顺序。分组在库里只是 Agent 上的一个字符串，
    所以还没有成员的分组名单独存一份（``agent.groups``）。
    """
    await guard_mutation(request, csrf_token)
    session = await current_session(request)
    state = request.app.state.xc

    async with state.sessionmaker() as s:
        try:
            created = await agent_svc.declare_group(s, state.keyring, name)
        except ValueError as e:
            put_agents_flash(request, session, "warn", str(e))
            return security_headers(RedirectResponse("/admin/agents", status_code=303))
        await s.commit()
    put_agents_flash(request, session, "ok", f"已建好分组「{created}」")
    return security_headers(RedirectResponse("/admin/agents", status_code=303))


@router.post("/agents/{slug}/toggle")
async def toggle_agent(request: Request, slug: str, csrf_token: str = Form(default="")) -> Response:
    """启用 / 停用。

    停用**不删**：调用方代码里写着这个 slug，删掉的话它们收到的是 model_not_found，
    而停用之后 slug 仍然被占着、不会被别的 Agent 顶替——那才是可逆的。
    """
    await guard_mutation(request, csrf_token)
    state = request.app.state.xc

    async with state.sessionmaker() as s:
        row = (await s.execute(select(AgentRow).where(AgentRow.slug == slug))).scalar_one_or_none()
        if row is not None:
            await agent_svc.set_active(s, row.id, not row.is_active)
            await s.commit()
    # 模型列表按 is_active 过滤，运行时缓存里可能还留着刚停用那个
    state.runtimes.clear()
    return security_headers(RedirectResponse("/admin/agents", status_code=303))


@router.get("/agents/new")
async def agent_new(request: Request) -> Response:
    await require_admin(request)

    state = request.app.state.xc
    async with state.sessionmaker() as s:
        # 新建页上的试运行还没有 slug，那几条归在空串下。
        history = [test_history_row(r) for r in await test_svc.recent(s, "")]
    return await page(
        request,
        "agent_form.html",
        {
            "is_new": True,
            "agent": None,
            "form": empty_form(),
            "action": "/admin/agents/save",
            "versions": [],
            "hints": [],
            "error": None,
            "saved": None,
            "history": history,
            **await form_shell(request),
        },
    )


@router.get("/agents/{slug}")
async def agent_edit(slug: str, request: Request) -> Response:
    session = await require_admin(request)
    state = request.app.state.xc

    async with state.sessionmaker() as s:
        resolved = await agent_svc.resolve(s, slug, include_inactive=True)
        row = await s.get(AgentRow, resolved.agent_id)
        group_name = row.group_name if row else None
        vers = await agent_svc.versions(s, resolved.agent_id)
        history = [test_history_row(r) for r in await test_svc.recent(s, slug)]
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
        group=group_name or "",
        **settings_view(spec),
    )

    return await page(
        request,
        "agent_form.html",
        {
            "is_new": False,
            "agent": resolved,
            # 停用的也进得来（include_inactive），所以页面上得看得出它现在是什么
            # 状态、并且能就地开回去——否则落在这一页的人只能再退回列表。
            "is_active": bool(row and row.is_active),
            "form": form,
            "action": "/admin/agents/save",
            "versions": version_rows,
            **await form_shell(request),
            "error": None,
            # 取走上一次保存的结果（经 flash 跨过 303）。一次性：刷新页面不再提示。
            "saved": take_saved(request, session),
            "history": history,
            **lint_ctx(form.schema, form.tier),
        },
    )


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
    group: str = Form(default=""),
    csrf_token: str = Form(default=""),
) -> Response:
    await guard_mutation(request, csrf_token)
    state = request.app.state.xc

    # 模型参数与能力用 ms_* / cap_* 前缀收，而不是逐个声明 Form 参数：
    # 字段清单由官方 schema 驱动（见 builder.FORM_MODEL_SETTINGS），逐个声明就等于
    # 把那份清单抄第二遍，而两份清单迟早会不一致。
    raw = await request.form()
    settings_raw = {
        field: str(raw.get(f"ms_{field}") or "")
        for field, *_ in (
            *builder.model_settings_fields(),
            *builder.choice_settings_fields(),
        )
    }
    caps = builder.capabilities_from_form(raw)
    if raw.get("instrument"):
        # 可观测就是 Instrumentation 这个 capability——不是 AgentSpec 的顶层字段
        caps.append(builder.CAPABILITY_INSTRUMENTATION)

    # 目录与 pydantic-ai 的 profile 都得点头，见 builder.native_ok。
    native_ok = builder.native_ok(
        model, state.provider, catalog_says=state.catalog.supports_native_schema(model)
    )
    try:
        prompting = prompting_from_form(raw)
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
                group_name=group,
            )
            await s.commit()
    except XingchaError as e:
        # 表单错误回到表单页并保留用户填的内容——跳到一个错误页会让人白填一遍。
        # 模型参数与能力也要保留：只回填前半截的话，用户会以为那些设置没生效。
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
            group=group,
            settings=settings_raw,
            has_settings=bool(filled),
            settings_count=len(filled),
            capabilities=builder.capability_names(caps),
            legacy_capabilities=sorted(
                builder.capability_names(caps)
                - {n for n, _, _, _ in builder.form_capabilities()}
                - {builder.CAPABILITY_INSTRUMENTATION}
            ),
            instrumented=builder.CAPABILITY_INSTRUMENTATION in builder.capability_names(caps),
            user_template=str(raw.get("user_template") or ""),
            output_channel=str(raw.get("output_channel") or "tool"),
            # 回填用户填的原文，而不是 validate_prompting 清洗过的版本：报错时把人
            # 填的东西改掉，会让他对着一个自己没写过的表单找错。
            examples=[
                SimpleNamespace(user=u, assistant=a)
                for u, a in zip_longest(
                    raw.getlist("ex_user"), raw.getlist("ex_assistant"), fillvalue=""
                )
            ],
        )
        return await page(
            request,
            "agent_form.html",
            {
                "is_new": True,
                "agent": None,
                "form": form,
                "action": "/admin/agents/save",
                "versions": [],
                "hints": [],
                "error": e.message,
                "saved": None,
                "history": [],
                **await form_shell(request),
            },
        )

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

    async with state.sessionmaker() as s:
        resolved = await agent_svc.resolve(s, slug, include_inactive=True)
        await agent_svc.rollback(s, resolved.agent_id, version)
        await s.commit()
    return security_headers(RedirectResponse(f"/admin/agents/{slug}", status_code=303))


@router.post("/agents/import")
async def agent_import(
    request: Request,
    bundle: UploadFile | None = File(default=None),
    yaml_text: str = Form(default=""),
    schema_text: str = Form(default=""),
    slug: str = Form(default=""),
    csrf_token: str = Form(default=""),
) -> Response:
    """导入一个 Agent。**「导出」的反向操作。**

    只有导出没有导入的话，那扇门是单向的——而「可带走」这个卖点要求两个方向都通。
    CLI 早就有 ``xingcha agent apply``，后台却没有，于是"改完导回来"这件事在页面上
    是做不到的。

    两种入口，对应两种真实用法：

    * **传 zip** —— 「导出」给你的就是它，原样传回来，不用先解压再找文件；
    * **贴 YAML** —— 在别处改了一份 ``agent.yaml``，或从 ``agent show`` 拷出来的。

    解析与字段映射走 :func:`services.agent.parse_bundle` / :func:`apply_bundle`，
    与 CLI 是**同一条代码路径**——两边各写一遍的话，其中一条总会先漏掉某个字段。
    """
    await guard_mutation(request, csrf_token)
    state = request.app.state.xc

    text, schema = yaml_text.strip(), schema_text.strip() or None
    default_slug = slug.strip() or None

    if bundle is not None and bundle.filename:
        raw = await bundle.read()
        if len(raw) > MAX_IMPORT_BYTES:
            raise Denied(f"文件太大（超过 {MAX_IMPORT_BYTES // 1024} KB）。")
        text, schema, default_slug = _read_bundle(raw, bundle.filename, default_slug)

    if not text:
        return await _import_error(request, "没有内容：上传导出的 zip，或把 agent.yaml 贴进来。")

    try:
        parsed = agent_svc.parse_bundle(text, slug=default_slug, schema_text=schema)
    except XingchaError as e:
        return await _import_error(request, e.message)

    # native_ok 用进程里已有的那份目录，不另外拉一次——切上游时它已经刷新过。
    # 查不到就按 False：判档退到 T2，**宁可保守**，错判成 T1 会让模型直接拒绝请求。
    native = False
    if state.provider is not None:
        native = builder.native_ok(
            parsed.model,
            state.provider,
            catalog_says=state.catalog.supports_native_schema(parsed.model),
        )

    try:
        async with state.sessionmaker() as s:
            result = await agent_svc.apply_bundle(
                s, parsed, native_ok=native, changelog="从后台导入"
            )
            await s.commit()
    except XingchaError as e:
        return await _import_error(request, e.message)

    session = await current_session(request)
    note = f"已导入「{result.slug}」→ v{result.version}（档位 {result.tier.value}）"
    if result.tier_note:
        note += f"；{result.tier_note}"
    put_agents_flash(request, session, "ok", note)
    return security_headers(RedirectResponse(f"/admin/agents/{result.slug}", status_code=303))


@router.get("/agents/{slug}/export")
async def agent_export(slug: str, request: Request) -> Response:
    """把 bundle 打成 zip 下载。

    在内存里打包而不是落临时文件：这些文件很小，而临时文件要考虑清理、并发同名、
    以及"进程被 kill 之后残留"——为一个几 KB 的下载引入那些不值得。
    """

    await require_admin(request)
    state = request.app.state.xc

    async with state.sessionmaker() as s:
        a = await agent_svc.resolve(s, slug, include_inactive=True)

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
