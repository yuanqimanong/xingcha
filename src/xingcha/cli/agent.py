"""``xingcha agent`` —— 查看、导出与导入 Agent。

``apply`` 吃的是 ``export`` 吐的那份 ``agent.yaml``，两者必须互为逆运算——
"改完能导回来"是「可带走」这个卖点的后半句。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml

from .. import contract as C
from ..core import builder, exporter
from ..core.models_catalog import ModelsCatalog
from ..core.upstream import UpstreamConfig, make_client
from ..db.engine import session_scope
from ..foundation.errors import XingchaError
from ..services import agent as agent_svc
from ..services import setting as setting_svc
from ._app import agent_app
from ._ui import bootstrap, err, info, ok, pad, run_async


@agent_app.command("apply")
def agent_apply(
    path: Annotated[Path, typer.Argument(help="agent.yaml（纯 AgentSpec）。传 `-` 从标准输入读。")],
    slug: Annotated[
        str | None, typer.Option("--slug", help="Agent 标识。默认取文件名所在目录名。")
    ] = None,
    schema: Annotated[
        Path | None,
        typer.Option("--schema", help="输出 JSON Schema。默认取同目录下的 schema.json。"),
    ] = None,
    tier: Annotated[
        str | None, typer.Option("--tier", help=f"请求档位：{'/'.join(t.value for t in C.Tier)}。")
    ] = None,
    group: Annotated[
        str | None,
        typer.Option("--group", help="分组名。不传保持原样；传空串挪回默认组。"),
    ] = None,
    changelog: Annotated[str, typer.Option("--changelog", help="这一版的说明。")] = "",
) -> None:
    """从 AgentSpec 文件新建或更新一个 Agent。

    这是 ``agent export`` 的反向操作，两者一起把 Agent 定义变成可版本管理的文件：

        xingcha agent export extract --dest ./agents
        $EDITOR ./agents/extract/agent.yaml
        xingcha agent apply ./agents/extract/agent.yaml

    **只有 export 没有 apply 的话，导出是一扇单向门**——能导出、不能导回来，
    而"低锁定"这个卖点恰恰要求两个方向都通。

    默认约定与 export 的产物对齐：``<dest>/<slug>/agent.yaml`` + 同目录的
    ``schema.json``，所以对着导出目录直接 apply 不用传任何选项。
    """

    if str(path) == "-":
        raw = sys.stdin.read()
        if slug is None:
            err("从标准输入读取时必须显式给 --slug。")
            raise typer.Exit(1)
        schema_text = None
    else:
        if not path.exists():
            err(f"文件不存在：{path}")
            raise typer.Exit(1)
        # 指着导出目录也认——上面那句 docstring 就是这么写的（"对着导出目录直接
        # apply 不用传任何选项"），而此前只认文件：传目录会以一句 PermissionError
        # 的 traceback 结束，看起来像权限问题，跟"路径给错了"毫无关系。
        if path.is_dir():
            candidate = path / "agent.yaml"
            if not candidate.exists():
                err(f"{path} 是个目录，但里面没有 agent.yaml。直接指到那个文件，或换个目录。")
                raise typer.Exit(1)
            path = candidate
        raw = path.read_text(encoding="utf-8")
        # 约定取自 export 的目录形状：<dest>/<slug>/agent.yaml
        slug = slug or path.parent.name
        schema_path = schema or (path.parent / "schema.json")
        schema_text = schema_path.read_text(encoding="utf-8") if schema_path.exists() else None

    if schema is not None and str(path) == "-":
        schema_text = schema.read_text(encoding="utf-8")

    # 解析与校验的规矩在 services/agent.parse_bundle 里，后台的「导入」走同一条。
    try:
        bundle = agent_svc.parse_bundle(raw, slug=slug, schema_text=schema_text)
    except XingchaError as e:
        err(e.message)
        raise typer.Exit(1) from e
    slug, model = bundle.slug, bundle.model

    async def run() -> object:
        engine, maker, keyring = bootstrap()
        try:
            # native_ok 要查模型目录（structured_outputs）。查不到就按 False，
            # 判档会退到 T2——**宁可保守**：错判成 T1 会让模型直接拒绝请求。
            native_ok = False
            catalog = ModelsCatalog(ttl_seconds=60)
            async with session_scope(maker) as s:  # type: ignore[arg-type]
                api_key = await setting_svc.get(s, keyring, C.SETTING_KEY_OPENROUTER_API_KEY)
                base_url = await setting_svc.get(s, keyring, C.SETTING_KEY_OPENROUTER_BASE_URL)
            if api_key:
                cfg = UpstreamConfig(
                    api_key=api_key,
                    base_url=base_url or C.OPENROUTER_DEFAULT_BASE_URL,
                )
                client = make_client(cfg, timeout=15.0)
                try:
                    if await catalog.refresh(client, cfg.api_key):
                        # 目录与 pydantic-ai 的 profile 都得点头，见 builder.native_ok。
                        native_ok = builder.native_ok(
                            model,
                            builder.make_provider(cfg, timeout=15.0),
                            catalog_says=catalog.supports_native_schema(model),
                        )
                finally:
                    await client.aclose()

            async with session_scope(maker) as s:  # type: ignore[arg-type]
                # 不传 --group 时**保持原样**。save() 把分组当表单里的一项处理
                # （不传 = 挪回默认组），那对后台的表单是对的；但命令行里"这次没提
                # 这一项"不等于"把它挪走"。照搬表单语义的话，每次 apply 都会静默把
                # Agent 踢回默认组，而 apply 的输出一个字都不提分组。
                group_name = await agent_svc.current_group(s, slug) if group is None else group
                result = await agent_svc.apply_bundle(
                    s,
                    bundle,
                    native_ok=native_ok,
                    requested_tier=C.Tier(tier) if tier else None,
                    group_name=group_name,
                    changelog=changelog,
                )
                await s.commit()
                return result
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    try:
        result = run_async(run())
    except XingchaError as e:
        err(e.message)
        raise typer.Exit(1) from e

    r: Any = result
    ok(f"已应用「{slug}」→ v{r.version}（档位 {r.tier.value}）")
    if r.tier_note:
        # 判档被降级时必须说出来。不说的话，用户以为拿到了 T1 的原生约束，
        # 实际跑的是 T2 的校验后重试——两者的失败形态完全不同。
        typer.secho(f"  · {r.tier_note}", fg=typer.colors.YELLOW)
    typer.secho("  运行时按版本缓存，新版本立即生效，不用重启。", fg=typer.colors.CYAN)


@agent_app.command("show")
def agent_show(
    slug: Annotated[str, typer.Argument(help="Agent 标识。")],
    schema: Annotated[bool, typer.Option("--schema", help="只输出 JSON Schema。")] = False,
) -> None:
    """打印一个 Agent 当前版本的 AgentSpec。

    输出是**可直接喂回 ``agent apply`` 的 YAML**，不是给人看的排版——
    这样 ``show | apply`` 与 ``export`` 的产物是同一种东西，只有一种格式要维护。
    """

    async def run() -> object:
        engine, maker, _ = bootstrap()
        try:
            async with session_scope(maker) as s:  # type: ignore[arg-type]
                return await agent_svc.resolve(s, slug, include_inactive=True)
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    try:
        a: Any = run_async(run())
    except XingchaError as e:
        err(e.message)
        raise typer.Exit(1) from e

    if schema:
        if not a.out_schema:
            err(f"「{slug}」是纯文本 Agent，没有输出 schema。")
            raise typer.Exit(1)
        typer.echo(json.dumps(json.loads(a.out_schema), ensure_ascii=False, indent=2))
        return

    typer.secho(
        f"# {a.slug} · v{a.version} · 档位 {a.tier.value}"
        f"{'（结构化）' if a.is_structured else '（纯文本）'}",
        fg=typer.colors.CYAN,
        err=True,  # 注释走 stderr，stdout 保持是干净的 YAML，可以直接管道
    )
    typer.echo(
        yaml.safe_dump(json.loads(a.spec_json), allow_unicode=True, sort_keys=False).rstrip()
    )


@agent_app.command("list")
def agent_list() -> None:
    """列出全部 Agent。"""

    async def run() -> list:
        engine, maker, _ = bootstrap()
        try:
            async with session_scope(maker) as s:  # type: ignore[arg-type]
                out = []
                for row, ver in await agent_svc.list_all(s):
                    spec = json.loads(ver.spec_json) if ver else {}
                    out.append(
                        (
                            row.slug,
                            row.name,
                            spec.get("model", "—"),
                            ver.tier if ver else "—",
                            bool(ver and ver.out_schema),
                            ver.version if ver else 0,
                            row.is_active,
                        )
                    )
                return out
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    rows = run_async(run())
    if not rows:
        info("还没有 Agent。在后台新建，或 `xingcha agent apply <file.yaml>`。")
        return
    typer.secho(
        pad("标识", 20) + pad("名称", 20) + pad("模型", 30) + pad("输出", 14) + "版本",
        bold=True,
    )
    for slug, name, model, tier, structured, version, active in rows:
        shape = f"{tier} 结构化" if structured else "纯文本"
        line = pad(slug, 20) + pad(name, 20) + pad(model, 30) + pad(shape, 14) + f"v{version}"
        typer.secho(line, fg=None if active else typer.colors.BRIGHT_BLACK)


@agent_app.command("export")
def agent_export(
    slug: Annotated[str, typer.Argument(help="Agent 标识。")],
    dest: Annotated[Path, typer.Option("--dest", help="导出到哪个目录。")] = Path("."),
) -> None:
    """导出成可脱离星槎运行的三文件目录。

    这是「低锁定」的兑现方式：产出物是标准的 pydantic-ai AgentSpec，不是星槎的
    私有格式。干净环境里只装 pydantic-ai-slim[openai,spec] 与 jsonschema 就能跑，
    **且校验行为一并保留**。
    """

    async def run() -> object:
        engine, maker, _ = bootstrap()
        try:
            async with session_scope(maker) as s:  # type: ignore[arg-type]
                a = await agent_svc.resolve(s, slug, include_inactive=True)
                return exporter.export(
                    slug=a.slug,
                    name=a.name,
                    version=a.version,
                    tier=a.tier,
                    spec=json.loads(a.spec_json),
                    out_schema=json.loads(a.out_schema) if a.out_schema else None,
                    dest=dest,
                )
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    try:
        bundle = run_async(run())
    except XingchaError as e:
        err(e.message)
        raise typer.Exit(1) from e

    ok(f"已导出 → {bundle.directory}")  # type: ignore[attr-defined]
    for f in bundle.files:  # type: ignore[attr-defined]
        typer.echo(f"    {f}")
    typer.secho(
        "  这个目录不依赖星槎。README.md 里写清了保留与丢失了什么。",
        fg=typer.colors.CYAN,
    )
