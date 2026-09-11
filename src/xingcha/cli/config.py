"""``xingcha config`` —— 读写服务端配置。

契约 §10 规定上游 key 不走环境变量，所以这一组是首次部署的唯一入口：
没有 ``config set``，新装的机器根本没有途径把 key 交给服务。
"""

from __future__ import annotations

import sys
from typing import Annotated

import typer

from .. import contract as C
from ..db.engine import session_scope
from ..services import setting as setting_svc
from ._app import config_app
from ._ui import bootstrap, err, info, ok, run_async

#: 这些配置项在**启动时**读一次，之后进程不再回看数据库。
#:
#: 后台的设置页改完会当场重装（它调 load_upstream / load_tracing），CLI 改不会——
#: CLI 是个独立进程，碰不到正在跑的那个。
_STARTUP_ONLY_KEYS = frozenset(
    {
        C.SETTING_KEY_OPENROUTER_API_KEY,
        C.SETTING_KEY_OPENROUTER_BASE_URL,
        C.SETTING_KEY_TRACE_ENDPOINT,
        C.SETTING_KEY_TRACE_PUBLIC_KEY,
        C.SETTING_KEY_TRACE_SECRET_KEY,
    }
)


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
            err("标准输入为空。")
            raise typer.Exit(1)

    async def run() -> None:
        engine, maker, keyring = bootstrap()
        try:
            async with session_scope(maker) as s:  # type: ignore[arg-type]
                await setting_svc.set_(s, keyring, key, value)
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    run_async(run())
    ok(f"已写入 {key}")
    _restart_hint(key)


def _restart_hint(key: str) -> None:
    """写完之后提醒重启。

    不提醒的话会出现最难受的一种失败：命令回了 ✓，服务照旧报"还没有配置
    OpenRouter API key"——而那句报错正好推荐了这条命令。用户会以为命令没生效、
    或者配置存错了地方，然后反复重试。
    """
    if key not in _STARTUP_ONLY_KEYS:
        return
    typer.secho(
        "  这一项在启动时读取，**需要重启才生效**：\n"
        "    docker compose restart xingcha        （容器部署）\n"
        "    systemctl restart xingcha             （或你自己的方式）\n"
        "  在后台的「设置」页改则当场生效，不用重启。",
        fg=typer.colors.YELLOW,
    )


@config_app.command("get")
def config_get(
    key: Annotated[str, typer.Argument()],
    reveal: Annotated[bool, typer.Option("--reveal", help="显示明文（默认脱敏）。")] = False,
) -> None:
    """读取一个配置项。默认脱敏显示。"""

    async def run() -> str | None:
        engine, maker, keyring = bootstrap()
        try:
            async with session_scope(maker) as s:  # type: ignore[arg-type]
                return await setting_svc.get(s, keyring, key)
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    value = run_async(run())

    if value is None:
        info(f"{key} 未设置")
        raise typer.Exit(1)
    typer.echo(value if reveal else setting_svc.mask(value))


@config_app.command("unset")
def config_unset(key: Annotated[str, typer.Argument()]) -> None:
    """删除一个配置项。"""

    async def run() -> bool:
        engine, maker, _ = bootstrap()
        try:
            async with session_scope(maker) as s:  # type: ignore[arg-type]
                return await setting_svc.unset(s, key)
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    removed = run_async(run())
    if removed:
        ok(f"已删除 {key}")
    else:
        info(f"{key} 本来就没有设置")


@config_app.command("list")
def config_list() -> None:
    """列出已设置的配置项。**不显示值。**"""

    async def run() -> list[tuple[str, bool, str]]:
        engine, maker, _ = bootstrap()
        try:
            async with session_scope(maker) as s:  # type: ignore[arg-type]
                return await setting_svc.list_keys(s)
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    rows = run_async(run())
    if not rows:
        info("还没有任何配置项")
        return
    for key, is_secret, updated in rows:
        # 标记用汉字而不是 🔒：Windows 控制台默认 GBK(936)，而 U+1F512 不在 GBK 里，
        # typer.echo 会直接抛 UnicodeEncodeError——**整条命令崩掉**，只为了一个装饰
        # 字符。Windows 是本项目支持的部署路径（deploy/windows/xc.bat），实际踩过。
        # 中文本身在 GBK 里，所以正文不受影响；换成别的 emoji 会重新踩一遍。
        tag = "密" if is_secret else "  "
        typer.echo(f"{tag} {key:<28} 更新于 {updated}")
