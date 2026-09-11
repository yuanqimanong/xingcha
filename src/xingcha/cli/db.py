"""``xingcha db`` —— 迁移、备份与恢复。

``downgrade`` 与 ``restore`` 都会先自动备份；``restore`` 前还跑一次
``PRAGMA integrity_check``。这一组的每条命令都可能是运维在事故中执行的，
所以宁可多做一步慢的，也不要让人手滑丢数据。
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from ..config import Settings, get_settings
from ..db import migrate
from ._app import db_app
from ._ui import err, info, ok, run_sync


@db_app.command("upgrade")
def db_upgrade() -> None:
    """升到最新 schema。迁移前自动备份。"""
    settings = get_settings()
    settings.ensure_data_dir()
    before, after = run_sync(migrate.upgrade_to_head, settings.db_path, settings.backup_dir)
    if before == after:
        info(f"已是最新（{after}）")
    else:
        ok(f"{before or '(空库)'} → {after}")


@db_app.command("downgrade")
def db_downgrade(
    revision: Annotated[str, typer.Argument(help="目标 revision，或 `base` 清空。")],
    yes: Annotated[bool, typer.Option("--yes", help="跳过确认。")] = False,
) -> None:
    """回退到指定 revision。**总是先备份。**"""
    settings = get_settings()
    if not yes:
        typer.confirm(f"确定把数据库回退到 {revision}？这会丢弃更高版本的数据。", abort=True)
    run_sync(migrate.downgrade_to, settings.db_path, revision, settings.backup_dir)
    ok(f"已回退到 {revision}")


@db_app.command("prune")
def db_prune(
    older_than: Annotated[int, typer.Option("--older-than", help="删掉这么多天之前的调用记录。")],
    yes: Annotated[bool, typer.Option("--yes", help="真的删。不加就是只看会删多少。")] = False,
) -> None:
    """删掉旧的调用记录。**默认只预览，不删。**

    run / run_usage 此前永远不删，一台长期跑着的星槎上它们只增不减——调用记录页
    越翻越慢、统计越算越久、迁移前的备份越拷越大。

    不自动删是因为这是**账单记录**："上个月到底花了多少"只有这张表答得出。一个
    自己会删账的系统，第一次被需要的时候正好是它已经删掉了的时候。
    """
    settings = get_settings()
    if older_than < 1:
        err("--older-than 至少是 1 天。")
        raise typer.Exit(1)

    doomed, kept = migrate.prune_runs(settings.db_path, older_than_days=older_than, dry_run=True)
    if not doomed:
        ok(f"{older_than} 天之前没有调用记录，什么都不用删（当前共 {kept} 行）。")
        return
    if not yes:
        typer.echo(
            f"会删掉 {doomed} 行（{older_than} 天之前），保留 {kept - doomed} 行。"
            "\n这是账单记录，删掉之后那段时间的费用就查不到了。"
            "\n确认请加 --yes；建议先 `xingcha db backup`。"
        )
        return

    migrate.backup(settings.db_path, settings.backup_dir, tag="pre-prune")
    deleted, left = migrate.prune_runs(settings.db_path, older_than_days=older_than, dry_run=False)
    ok(f"已删除 {deleted} 行，剩余 {left} 行。删除前的备份在 {settings.backup_dir}。")


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
        err("数据库还不存在，没有可备份的内容。")
        raise typer.Exit(1)
    ok(f"已备份 → {dest}")
    typer.secho(
        f"  记得单独备份密钥环：{settings.secret_path}（不在上面这个文件里）",
        fg=typer.colors.YELLOW,
    )


@db_app.command("verify")
def db_verify(
    backup_path: Annotated[
        Path | None,
        typer.Argument(help="备份文件路径。不传则检查 data/backups/ 里最新的一份。"),
    ] = None,
) -> None:
    """体检一份备份：能不能打开、完整不完整、里面有什么。

    **「备份文件在那儿」不等于「备份能用」。** 灾难当天才发现备份是坏的，等于从来
    没有备份过——所以这条命令的用法是**定期跑**，不是出事之后再跑。

    它只读不写，可以随时对生产库的备份执行。
    """
    settings = get_settings()
    target = backup_path or _latest_backup(settings)
    if target is None:
        err(f"{settings.backup_dir} 里没有任何备份。先跑 `xingcha db backup`。")
        raise typer.Exit(1)

    report = migrate.verify_backup(target)
    info(f"检查 {report.path}")
    typer.echo(f"  大小      {report.size_bytes / 1024:.1f} KiB")
    typer.echo(f"  完整性    {'ok' if report.integrity_ok else '不通过'}")
    typer.echo(f"  schema    {report.revision or '(无)'}")
    if report.counts:
        rows = ", ".join(f"{k}={v}" for k, v in sorted(report.counts.items()) if v)
        typer.echo(f"  行数      {rows or '(全空)'}")

    for problem in report.problems:
        err(problem)

    if report.ciphertext_rows:
        typer.secho(
            f"\n  这份备份里有 {report.ciphertext_rows} 条密文（上游 key 等）。"
            f"\n  它们要靠密钥环才能解开，而密钥环**不在这个文件里**："
            f"\n    {settings.secret_path}"
            "\n  只恢复数据库不恢复密钥环，服务会拒绝启动（这是有意的——"
            "\n  静默重新生成会让密文永久解不开）。两者都要备份。",
            fg=typer.colors.YELLOW,
        )

    if not report.usable:
        raise typer.Exit(1)
    ok("这份备份可用")


def _latest_backup(settings: Settings) -> Path | None:
    if not settings.backup_dir.exists():
        return None
    files = sorted(settings.backup_dir.glob("*.db"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


@db_app.command("restore")
def db_restore(
    backup_path: Annotated[Path, typer.Argument(help="备份文件路径。")],
    yes: Annotated[bool, typer.Option("--yes", help="跳过确认。")] = False,
) -> None:
    """从备份恢复。会覆盖当前数据库。"""
    settings = get_settings()
    if not yes:
        typer.confirm(f"确定用 {backup_path} 覆盖 {settings.db_path}？", abort=True)
    run_sync(migrate.restore, backup_path, settings.db_path)
    ok("已恢复")
