"""``xingcha token`` —— 签发、查看与吊销 ``sk-xc-`` 令牌。"""

from __future__ import annotations

from typing import Annotated

import typer

from .. import contract as C
from ..db.engine import session_scope
from ..services import auth as auth_svc
from ._app import token_app
from ._ui import bootstrap, err, info, ok, pad, run_async


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
        engine, maker, _ = bootstrap()
        try:
            async with session_scope(maker) as s:  # type: ignore[arg-type]
                return await auth_svc.issue(
                    s, name=name.strip()[:60] or "未命名", expires_at=auth_svc.parse_expiry(days)
                )
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    issued: auth_svc.IssuedToken = run_async(run())
    ok(f"已签发「{issued.name}」（{issued.display_prefix}）")
    typer.secho("  下面这行是明文，只显示这一次：", fg=typer.colors.YELLOW, err=True)
    # 明文单独走 stdout 且不带任何装饰，方便 `| tail -1` 直接取用；
    # 提示语走 stderr，这样管道里不会混进人类可读的文字。
    typer.echo(issued.plaintext)


@token_app.command("list")
def token_list() -> None:
    """列出全部令牌。**不显示明文**——库里根本没有。"""

    async def run() -> list:
        engine, maker, _ = bootstrap()
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

    rows = run_async(run())
    if not rows:
        info("还没有签发过任何令牌。签发：xingcha token issue <用途>")
        return

    typer.secho(
        pad("用途", 20) + pad("标识", 26) + pad("状态", 8) + pad("最后使用", 22) + "创建",
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
            pad(name, 20)
            + pad(prefix, 26)
            + pad(state, 8)
            + pad((last_used or "—")[:19], 22)
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
        engine, maker, _ = bootstrap()
        try:
            async with session_scope(maker) as s:  # type: ignore[arg-type]
                return await auth_svc.revoke(s, kid)
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    if run_async(run()):
        ok(f"已吊销 {kid}")
    else:
        err(f"没有找到可吊销的令牌 {kid}（不存在，或已经是吊销状态）")
        raise typer.Exit(1)
