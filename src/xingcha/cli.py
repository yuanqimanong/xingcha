"""命令行。

命令集是**闭集**（契约 §3.13）：脚本会依赖这些命令名，改名等于毁约。

``config set`` 必须在第一个版本就可用——按契约 §3.11「上游 key 不走环境变量」，
没有它的话首次部署根本没有途径设置 key，服务起不来。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Annotated

import typer

from . import __version__
from . import contract as C
from .bootstrap import prepare
from .config import get_settings
from .crypto import Keyring, KeyringInvalid, KeyringMissing
from .db import migrate
from .db.engine import StartupRefused, make_engine, make_sessionmaker, session_scope
from .services import auth as auth_svc
from .services import setting as setting_svc

app = typer.Typer(
    name="xingcha",
    help="星槎 —— 把提示词变成可调用、可计量、可带走的服务。",
    no_args_is_help=True,
    add_completion=False,
)
config_app = typer.Typer(help="读写服务端配置（上游 key 等）。", no_args_is_help=True)
db_app = typer.Typer(help="数据库迁移与备份。", no_args_is_help=True)
token_app = typer.Typer(help="签发、查看与吊销 API 令牌。", no_args_is_help=True)
app.add_typer(config_app, name="config")
app.add_typer(db_app, name="db")
app.add_typer(token_app, name="token")


def _err(msg: str) -> None:
    typer.secho(f"✗ {msg}", fg=typer.colors.RED, err=True)


def _ok(msg: str) -> None:
    typer.secho(f"✓ {msg}", fg=typer.colors.GREEN)


def _info(msg: str) -> None:
    typer.secho(f"→ {msg}", fg=typer.colors.CYAN)


def _width(text: str) -> int:
    """字符串在终端里占几列。

    CJK 与全角标点占两列。用 ``len()`` 对齐中文表格会把列撑歪——而这个后台的
    用途名基本都是中文。
    """
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(text: str, columns: int) -> str:
    """按显示宽度左对齐填充。超宽则截断并留一个空格分隔。"""
    w = _width(text)
    if w >= columns:
        out = ""
        used = 0
        for ch in text:
            cw = _width(ch)
            if used + cw > columns - 1:
                break
            out += ch
            used += cw
        return out + " " * (columns - used)
    return text + " " * (columns - w)


#: 这些是「运维需要动手处理」的失败，不是程序缺陷。
#:
#: 对它们甩 traceback 是没有意义的——栈帧不会告诉运维该做什么，而这些异常的消息里
#: 恰恰写着该做什么。所以在 CLI 边界上把它们转成干净的提示。
_OPERATIONAL_ERRORS = (KeyringMissing, KeyringInvalid, StartupRefused)


def _run(coro):
    """跑一个异步命令，把运维类失败转成干净的退出。"""
    try:
        return asyncio.run(coro)
    except _OPERATIONAL_ERRORS as e:
        _err(str(e))
        raise typer.Exit(2) from e
    except setting_svc.UnknownSettingKey as e:
        _err(str(e).strip("\"'"))
        raise typer.Exit(1) from e


def _run_sync(fn, *args, **kwargs):
    """同上，用于同步命令。"""
    try:
        return fn(*args, **kwargs)
    except _OPERATIONAL_ERRORS as e:
        _err(str(e))
        raise typer.Exit(2) from e


# =============================================================================
# serve
# =============================================================================


@app.command()
def serve(
    host: Annotated[str | None, typer.Option(help="监听地址。默认 127.0.0.1。")] = None,
    port: Annotated[int | None, typer.Option(help="监听端口。")] = None,
    reload: Annotated[bool, typer.Option(help="改代码自动重启（仅开发用）。")] = False,
) -> None:
    """启动服务。

    启动时会自动跑数据库迁移（迁移前先备份），并做一系列拒绝启动的断言——
    见 app.lifespan 的注释。
    """
    import uvicorn

    settings = get_settings()
    bind_host = host or settings.host
    bind_port = port or settings.port

    if bind_host == "0.0.0.0":
        typer.secho(
            "⚠ 正在监听 0.0.0.0。星槎默认只监听 127.0.0.1，生产环境应由 Caddy 前置、"
            "容器不映射宿主端口。\n"
            "  注意 Docker 的 DOCKER-USER 链会绕过 ufw：即使防火墙规则写了 deny，"
            "映射出去的端口照样可达。",
            fg=typer.colors.YELLOW,
            err=True,
        )

    uvicorn.run(
        "xingcha.app:create_app",
        factory=True,
        host=bind_host,
        port=bind_port,
        reload=reload,
        # 单 worker 是硬约束：进程级并发上限、用量缓冲、SQLite 单写者全都依赖它。
        workers=C.REQUIRED_WORKERS,
        log_config=None,
        access_log=False,  # access log 会把 header 写进日志，那是一条 key 泄漏路径
    )


# =============================================================================
# config
# =============================================================================


def _bootstrap() -> tuple[object, object, Keyring]:
    """CLI 用的最小上下文：引擎 + 密钥环。不启动 web 层。

    走的是与 serve 完全相同的 bootstrap.prepare——包括「密钥环缺失且库里已有密文时
    拒绝新建」那道守卫。CLI 自己再实现一遍的话，一次 `config get` 就能绕过它。
    """
    settings = get_settings()
    keyring = prepare(settings)
    engine = make_engine(settings.db_path)
    return engine, make_sessionmaker(engine), keyring


@config_app.command("set")
def config_set(
    key: Annotated[str, typer.Argument(help=f"配置项。可用：{sorted(setting_svc.KNOWN_KEYS)}")],
    value: Annotated[
        str,
        typer.Argument(help="值。传 `-` 从标准输入读取（避免 key 落进 shell 历史）。"),
    ],
) -> None:
    """写入一个配置项。敏感值会 Fernet 加密后落库。

    例：

        xingcha config set openrouter.api_key -

    用 `-` 从 stdin 读，key 就不会出现在 shell 历史与 ps 输出里。
    """
    if value == "-":
        value = sys.stdin.read().strip()
        if not value:
            _err("标准输入为空。")
            raise typer.Exit(1)

    async def run() -> None:
        engine, maker, keyring = _bootstrap()
        try:
            async with session_scope(maker) as s:  # type: ignore[arg-type]
                await setting_svc.set_(s, keyring, key, value)
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    _run(run())
    _ok(f"已写入 {key}")


@config_app.command("get")
def config_get(
    key: Annotated[str, typer.Argument()],
    reveal: Annotated[bool, typer.Option("--reveal", help="显示明文（默认脱敏）。")] = False,
) -> None:
    """读取一个配置项。默认脱敏显示。"""

    async def run() -> str | None:
        engine, maker, keyring = _bootstrap()
        try:
            async with session_scope(maker) as s:  # type: ignore[arg-type]
                return await setting_svc.get(s, keyring, key)
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    value = _run(run())

    if value is None:
        _info(f"{key} 未设置")
        raise typer.Exit(1)
    typer.echo(value if reveal else setting_svc.mask(value))


@config_app.command("unset")
def config_unset(key: Annotated[str, typer.Argument()]) -> None:
    """删除一个配置项。"""

    async def run() -> bool:
        engine, maker, _ = _bootstrap()
        try:
            async with session_scope(maker) as s:  # type: ignore[arg-type]
                return await setting_svc.unset(s, key)
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    removed = _run(run())
    if removed:
        _ok(f"已删除 {key}")
    else:
        _info(f"{key} 本来就没有设置")


@config_app.command("list")
def config_list() -> None:
    """列出已设置的配置项。**不显示值。**"""

    async def run() -> list[tuple[str, bool, str]]:
        engine, maker, _ = _bootstrap()
        try:
            async with session_scope(maker) as s:  # type: ignore[arg-type]
                return await setting_svc.list_keys(s)
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    rows = _run(run())
    if not rows:
        _info("还没有任何配置项")
        return
    for key, is_secret, updated in rows:
        tag = "🔒" if is_secret else "  "
        typer.echo(f"{tag} {key:<28} 更新于 {updated}")


# =============================================================================
# token
# =============================================================================


@token_app.command("issue")
def token_issue(
    name: Annotated[str, typer.Argument(help="用途说明，例如「本地开发」。")],
    days: Annotated[int | None, typer.Option("--days", help="有效期天数。不传则永不过期。")] = None,
) -> None:
    """签发一把新令牌。

    **明文只在这一刻打印一次**，之后不可恢复——库里只有哈希与一个不可推导的标识。
    丢了就吊销重签，成本很低。

    无头部署（没有浏览器）时这是唯一的签发途径。管道友好：

        xingcha token issue ci --days 90 | tail -1 > /run/secrets/xc-key
    """

    async def run() -> auth_svc.IssuedToken:
        engine, maker, _ = _bootstrap()
        try:
            async with session_scope(maker) as s:  # type: ignore[arg-type]
                return await auth_svc.issue(
                    s, name=name.strip()[:60] or "未命名", expires_at=auth_svc.parse_expiry(days)
                )
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    issued: auth_svc.IssuedToken = _run(run())
    _ok(f"已签发「{issued.name}」（{issued.display_prefix}）")
    typer.secho("  下面这行是明文，只显示这一次：", fg=typer.colors.YELLOW, err=True)
    # 明文单独走 stdout 且不带任何装饰，方便 `| tail -1` 直接取用；
    # 提示语走 stderr，这样管道里不会混进人类可读的文字。
    typer.echo(issued.plaintext)


@token_app.command("list")
def token_list() -> None:
    """列出全部令牌。**不显示明文**——库里根本没有。"""

    async def run() -> list:
        engine, maker, _ = _bootstrap()
        try:
            async with session_scope(maker) as s:  # type: ignore[arg-type]
                rows = await auth_svc.list_tokens(s)
                # 会话关闭后属性会失效，所以在这里取干净的值出来
                return [
                    (
                        t.name,
                        t.kid,
                        t.display_prefix,
                        t.is_active,
                        auth_svc.is_expired(t),
                        t.last_used_at,
                        t.created_at,
                    )
                    for t in rows
                ]
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    rows = _run(run())
    if not rows:
        _info("还没有签发过任何令牌。签发：xingcha token issue <用途>")
        return

    typer.secho(
        _pad("用途", 20) + _pad("标识", 26) + _pad("状态", 8) + _pad("最后使用", 22) + "创建",
        bold=True,
    )
    for name, _kid, prefix, active, expired, last_used, created in rows:
        if not active:
            state, color = "已吊销", typer.colors.BRIGHT_BLACK
        elif expired:
            state, color = "已过期", typer.colors.YELLOW
        else:
            state, color = "可用", typer.colors.GREEN
        typer.secho(
            _pad(name, 20)
            + _pad(prefix, 26)
            + _pad(state, 8)
            + _pad((last_used or "—")[:19], 22)
            + created[:19],
            fg=color,
        )


@token_app.command("revoke")
def token_revoke(
    kid: Annotated[str, typer.Argument(help="令牌标识（token list 里那一列）。")],
    yes: Annotated[bool, typer.Option("--yes", help="跳过确认。")] = False,
) -> None:
    """吊销令牌。立刻生效——使用它的调用会马上开始返回 401。

    置为不可用而不是删行：删掉之后历史调用记录就找不到归属了。
    """
    # 允许直接粘贴完整的 display_prefix（sk-xc-1-<kid>），省得手动截取
    if kid.startswith(C.TOKEN_PREFIX):
        kid = kid.rsplit("-", 1)[-1]

    if not yes:
        typer.confirm(f"确定吊销 {kid}？使用它的调用会立刻开始返回 401。", abort=True)

    async def run() -> bool:
        engine, maker, _ = _bootstrap()
        try:
            async with session_scope(maker) as s:  # type: ignore[arg-type]
                return await auth_svc.revoke(s, kid)
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    if _run(run()):
        _ok(f"已吊销 {kid}")
    else:
        _err(f"没有找到可吊销的令牌 {kid}（不存在，或已经是吊销状态）")
        raise typer.Exit(1)


# =============================================================================
# db
# =============================================================================


@db_app.command("upgrade")
def db_upgrade() -> None:
    """升到最新 schema。迁移前自动备份。"""
    settings = get_settings()
    settings.ensure_data_dir()
    before, after = _run_sync(migrate.upgrade_to_head, settings.db_path, settings.backup_dir)
    if before == after:
        _info(f"已是最新（{after}）")
    else:
        _ok(f"{before or '(空库)'} → {after}")


@db_app.command("downgrade")
def db_downgrade(
    revision: Annotated[str, typer.Argument(help="目标 revision，或 `base` 清空。")],
    yes: Annotated[bool, typer.Option("--yes", help="跳过确认。")] = False,
) -> None:
    """回退到指定 revision。**总是先备份。**"""
    settings = get_settings()
    if not yes:
        typer.confirm(f"确定把数据库回退到 {revision}？这会丢弃更高版本的数据。", abort=True)
    _run_sync(migrate.downgrade_to, settings.db_path, revision, settings.backup_dir)
    _ok(f"已回退到 {revision}")


@db_app.command("backup")
def db_backup(
    tag: Annotated[str, typer.Option(help="备份文件名里的标记。")] = "",
) -> None:
    """用 VACUUM INTO 做一份崩溃一致的备份。

    注意备份**不含**密钥环。`data/secret.key` 必须单独备份——把密文和密钥打进同一个包，
    等于让加密对「备份泄露」这个最现实的威胁提供零保护。
    """
    settings = get_settings()
    dest = migrate.backup(settings.db_path, settings.backup_dir, tag=tag)
    if dest is None:
        _err("数据库还不存在，没有可备份的内容。")
        raise typer.Exit(1)
    _ok(f"已备份 → {dest}")
    typer.secho(
        f"  记得单独备份密钥环：{settings.secret_path}（不在上面这个文件里）",
        fg=typer.colors.YELLOW,
    )


@db_app.command("restore")
def db_restore(
    backup_path: Annotated[Path, typer.Argument(help="备份文件路径。")],
    yes: Annotated[bool, typer.Option("--yes", help="跳过确认。")] = False,
) -> None:
    """从备份恢复。会覆盖当前数据库。"""
    settings = get_settings()
    if not yes:
        typer.confirm(f"确定用 {backup_path} 覆盖 {settings.db_path}？", abort=True)
    _run_sync(migrate.restore, backup_path, settings.db_path)
    _ok("已恢复")


# =============================================================================
# doctor
# =============================================================================


@app.command()
def doctor() -> None:
    """体检：把常见的部署问题一次性查出来。"""
    settings = get_settings()
    problems = 0

    typer.secho(f"星槎 {__version__} · 契约 v{C.CONTRACT_VERSION}", bold=True)
    typer.echo()

    # --- 数据目录 ---
    typer.secho("数据", bold=True)
    typer.echo(f"  目录        {settings.data_dir.resolve()}")
    if settings.data_dir.exists():
        import stat as st

        mode = st.S_IMODE(settings.data_dir.stat().st_mode)
        flag = "ok" if mode == C.DIR_MODE else f"应为 {C.DIR_MODE:o}"
        typer.echo(f"  目录权限    {mode:o} ({flag})")
        if mode != C.DIR_MODE:
            problems += 1
    else:
        typer.echo("  目录        尚未创建（首次 serve 时会建）")

    rev = migrate.current_revision(settings.db_path)
    head = migrate.head_revision()
    typer.echo(f"  schema      {rev or '(空库)'} / head {head}")
    if rev != head:
        typer.secho("              未迁移到最新，serve 启动时会自动升级", fg=typer.colors.YELLOW)

    typer.echo(f"  密钥环      {'存在' if settings.secret_path.exists() else '尚未创建'}")

    # --- 磁盘 ---
    try:
        import shutil

        u = shutil.disk_usage(settings.data_dir if settings.data_dir.exists() else Path("."))
        pct = u.free / u.total * 100
        typer.echo(f"  磁盘剩余    {u.free // 1024 // 1024} MB ({pct:.1f}%)")
        if pct < 10:
            typer.secho(
                "              磁盘不足 10%，SQLite 写入与迁移都会失败", fg=typer.colors.RED
            )
            problems += 1
    except OSError:
        pass

    # --- 网络与代理 ---
    typer.echo()
    typer.secho("网络", bold=True)
    import os

    proxy_vars = {
        k: v
        for k, v in os.environ.items()
        if k.lower() in {"all_proxy", "http_proxy", "https_proxy"}
    }
    if proxy_vars:
        typer.secho(
            "  检测到机器级代理环境变量：" + ", ".join(sorted(proxy_vars)),
            fg=typer.colors.YELLOW,
        )
        typer.echo("    星槎自建的 HTTP 客户端一律 trust_env=False，不会继承它们。")
        typer.echo("    要走中转请配 openrouter.base_url，不要依赖机器代理。")
        if any(v.startswith("socks") for v in proxy_vars.values()):
            typer.secho(
                "    注意：socks5 代理会让未关闭 trust_env 的客户端在构造阶段直接 "
                "ImportError（socksio 未装），且报错完全看不出跟代理有关。",
                fg=typer.colors.YELLOW,
            )
    else:
        typer.echo("  未检测到机器级代理环境变量")

    base_url = C.OPENROUTER_DEFAULT_BASE_URL
    typer.echo(f"  上游默认    {base_url}")

    # --- 运行约束 ---
    typer.echo()
    typer.secho("运行约束", bold=True)
    typer.echo(f"  worker      {C.REQUIRED_WORKERS}（硬约束，启动时断言）")
    typer.echo(f"  journal     {C.REQUIRED_JOURNAL_MODE}（启动时断言，否则拒绝启动）")
    typer.echo(f"  监听        {settings.host}:{settings.port}")
    typer.echo(f"  请求体上限  {C.MAX_BODY_BYTES // 1024 // 1024} MB")
    typer.echo(f"  并发上限    {settings.max_concurrency}")

    typer.echo()
    if problems:
        typer.secho(f"发现 {problems} 个问题。", fg=typer.colors.RED)
        raise typer.Exit(1)
    _ok("没有发现问题。")


@app.command()
def version() -> None:
    """打印版本与契约号。"""
    typer.echo(f"xingcha {__version__} (contract v{C.CONTRACT_VERSION})")


if __name__ == "__main__":
    app()
