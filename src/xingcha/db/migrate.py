"""迁移的编程入口。

星槎不用 ``alembic.ini``——迁移目录随 wheel 分发，配置由代码构造。这样 ``xingcha serve``
在任何工作目录下都能自动 ``upgrade head``，不需要用户 cd 到某个特定位置。

**备份先于迁移。** :func:`upgrade_to_head` 在真正执行迁移之前先做一次 ``VACUUM INTO``
备份。用 ``VACUUM INTO`` 而不是 ``cp``：WAL 模式下直接复制活库**不是崩溃一致的**
（``-wal`` 里可能还有未 checkpoint 的事务），拷出来的文件可能根本打不开。
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from .. import contract as C

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def make_alembic_config(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def current_revision(db_path: Path) -> str | None:
    """库当前所在的 revision；全新库或未打过迁移的库返回 None。"""
    if not db_path.exists():
        return None
    engine = sa.create_engine(f"sqlite:///{db_path}", poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as conn:
            return MigrationContext.configure(conn).get_current_revision()
    finally:
        engine.dispose()


def head_revision() -> str:
    return ScriptDirectory.from_config(make_alembic_config(Path("unused"))).get_current_head() or ""


def backup(db_path: Path, backup_dir: Path, *, tag: str = "") -> Path | None:
    """用 ``VACUUM INTO`` 做一份崩溃一致的备份。库不存在则返回 None。

    注意备份里含 token hash 与 Fernet 密文，但**不含**密钥环——
    ``data/secret.key`` 必须单独备份。把密文和密钥打进同一个包，等于让 setting 表的
    加密对「备份泄露」这个最现实的威胁提供零保护。
    """
    if not db_path.exists():
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    suffix = f"-{tag}" if tag else ""
    dest = backup_dir / f"xingcha-{stamp}{suffix}.db"
    with sqlite3.connect(db_path) as conn:
        # VACUUM INTO 的目标必须不存在
        if dest.exists():
            dest.unlink()
        conn.execute("VACUUM INTO ?", (str(dest),))
    dest.chmod(C.FILE_MODE)
    log.info("已备份数据库 → %s", dest)
    return dest


def restore(backup_path: Path, db_path: Path) -> None:
    """从备份恢复。会覆盖现有库，调用方负责确认。"""
    if not backup_path.exists():
        raise FileNotFoundError(f"备份文件不存在：{backup_path}")
    # 先验证备份本身能打开，别用一个坏文件覆盖好库
    with sqlite3.connect(backup_path) as conn:
        result = conn.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise RuntimeError(f"备份文件损坏（integrity_check = {result}），拒绝恢复")
    for suffix in ("-wal", "-shm"):
        stale = db_path.with_name(db_path.name + suffix)
        stale.unlink(missing_ok=True)
    shutil.copy2(backup_path, db_path)
    db_path.chmod(C.FILE_MODE)
    log.info("已从 %s 恢复数据库", backup_path)


@dataclass(frozen=True)
class BackupReport:
    """一份备份的体检结果。

    "备份文件在那儿"不等于"备份能用"。这个报告要回答的是后者，而且要在**真需要
    它之前**回答——灾难当天才发现备份是坏的，等于没有备份。
    """

    path: Path
    size_bytes: int
    integrity_ok: bool
    revision: str | None
    counts: dict[str, int]
    #: 库里有多少条密文。**它们要靠密钥环才能解开，而密钥环不在这份备份里。**
    ciphertext_rows: int
    problems: list[str]

    @property
    def usable(self) -> bool:
        return not self.problems


def verify_backup(backup_path: Path, *, expect_tables: tuple[str, ...] = ()) -> BackupReport:
    """体检一份备份，不改动任何东西。

    检查的顺序按"越致命越先"排：文件在不在 → 打不打得开 → 完整性 → schema 版本
    → 有没有内容。任何一项失败都进 ``problems``，一条都不省——只报第一个问题的话，
    运维得来回跑好几遍才能看清全貌。
    """
    problems: list[str] = []
    if not backup_path.exists():
        return BackupReport(backup_path, 0, False, None, {}, 0, [f"文件不存在：{backup_path}"])

    size = backup_path.stat().st_size
    integrity_ok = False
    revision: str | None = None
    counts: dict[str, int] = {}
    ciphertext = 0

    try:
        with sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True) as conn:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            integrity_ok = bool(row and row[0] == "ok")
            if not integrity_ok:
                problems.append(f"完整性检查未通过：{row[0] if row else '(无结果)'}")

            tables = {
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "alembic_version" in tables:
                v = conn.execute("SELECT version_num FROM alembic_version").fetchone()
                revision = v[0] if v else None
            if revision is None:
                problems.append("没有 alembic_version——这不像是星槎的库")

            for name in expect_tables or tuple(sorted(tables - {"alembic_version"})):
                if name not in tables:
                    problems.append(f"缺表：{name}")
                    continue
                counts[name] = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]

            if "setting" in tables:
                ciphertext = conn.execute(
                    "SELECT COUNT(*) FROM setting WHERE is_secret = 1 AND value_enc IS NOT NULL"
                ).fetchone()[0]
    except sqlite3.DatabaseError as e:
        problems.append(f"打不开：{e}")

    return BackupReport(backup_path, size, integrity_ok, revision, counts, ciphertext, problems)


def upgrade_to_head(db_path: Path, backup_dir: Path | None = None) -> tuple[str | None, str]:
    """把库升到最新 revision。返回 ``(升级前, 升级后)``。

    已在最新版时直接返回，不做备份、不做任何写入——每次重启都备份一次会很快塞满磁盘。
    """
    before = current_revision(db_path)
    head = head_revision()
    if before == head:
        return before, head

    if backup_dir is not None and before is not None:
        backup(db_path, backup_dir, tag=f"pre-{head}")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    command.upgrade(make_alembic_config(db_path), "head")
    if db_path.exists():
        db_path.chmod(C.FILE_MODE)
    after = current_revision(db_path) or ""
    log.info("数据库迁移 %s → %s", before or "(空库)", after)
    return before, after


def downgrade_to(db_path: Path, revision: str, backup_dir: Path | None = None) -> None:
    """回退到指定 revision。**总是**先备份——回退是破坏性的。"""
    if backup_dir is not None:
        backup(db_path, backup_dir, tag=f"pre-downgrade-{revision}")
    command.downgrade(make_alembic_config(db_path), revision)
