"""迁移与 schema 的约束级测试。

**只断言"列存在"是不够的。** SQLite 给已有列加 NOT NULL / UNIQUE 要走
「建新表 → 拷数据 → 换名」，在有真实数据的线上库上就是停机迁移——而这恰好被
「重启 + 自动迁移」的升级档位禁掉。所以这些约束必须在 0001 就对，而这里逐条验证。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from xingcha import contract as C
from xingcha.db import migrate


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "xingcha.db"
    migrate.upgrade_to_head(p, tmp_path / "backups")
    return p


def _conn(db: Path) -> sqlite3.Connection:
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return c


def _column(conn: sqlite3.Connection, table: str, name: str) -> sqlite3.Row:
    for row in conn.execute(f"PRAGMA table_info({table})"):
        if row["name"] == name:
            return row
    raise AssertionError(f"{table}.{name} 不存在")


def _table_sql(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()
    assert row is not None, f"表 {table} 不存在"
    return row["sql"]


# =============================================================================
# 迁移链
# =============================================================================


class TestMigrationChain:
    def test_fresh_upgrade(self, tmp_path: Path):
        p = tmp_path / "x.db"
        before, after = migrate.upgrade_to_head(p, tmp_path / "b")
        assert before is None
        assert after == migrate.head_revision()

    def test_upgrade_is_idempotent(self, db: Path, tmp_path: Path):
        """每次重启都会跑一次 upgrade；已在最新版时必须什么都不做。"""
        before, after = migrate.upgrade_to_head(db, tmp_path / "backups")
        assert before == after == migrate.head_revision()

    def test_downgrade_then_upgrade(self, db: Path, tmp_path: Path):
        """回退必须可用。

        一个人在生产上跑迁移却没有已演练的回头路，是不可接受的——所以每个 revision
        都必须有能跑通的 downgrade()，而不只是一个 pass。
        """
        migrate.downgrade_to(db, "base", tmp_path / "backups")
        assert migrate.current_revision(db) is None
        with _conn(db) as c:
            names = {
                r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        assert "agent" not in names, "downgrade 没有清干净"

        migrate.upgrade_to_head(db, tmp_path / "backups")
        assert migrate.current_revision(db) == migrate.head_revision()

    def test_all_expected_tables(self, db: Path):
        with _conn(db) as c:
            names = {
                r["name"]
                for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")
                if not r["name"].startswith(("sqlite_", "alembic_"))
            }
        assert names == {
            "setting",
            "user",
            "token",
            "agent",
            "agent_alias",
            "agent_version",
            "run",
            "run_usage",
            "quota",
            "web_session",
        }


# =============================================================================
# 事后补 = 停机 的那些约束
# =============================================================================


class TestHardToAddLaterConstraints:
    def test_user_row_is_seeded(self, db: Path):
        """v1 单用户，但所有主体表 NOT NULL 引用 user(id)。

        没有这一行的话，NOT NULL + 外键会让第一次插入就失败。
        """
        with _conn(db) as c:
            rows = c.execute("SELECT id, username, role FROM user").fetchall()
        assert len(rows) == 1
        assert (rows[0]["id"], rows[0]["username"], rows[0]["role"]) == (1, "admin", "admin")

    @pytest.mark.parametrize(
        ("table", "column"),
        [
            ("agent", "user_id"),
            ("agent_version", "user_id"),
            ("run", "user_id"),
            ("token", "user_id"),
            ("web_session", "user_id"),
        ],
    )
    def test_user_id_is_not_null(self, db: Path, table: str, column: str):
        """v2 加多用户时不能停机。SQLite 事后加 NOT NULL 要重建表。"""
        with _conn(db) as c:
            assert _column(c, table, column)["notnull"] == 1

    def test_agent_slug_is_globally_unique(self, db: Path):
        """slug 是全局命名空间，不是 per-user。

        若 v2 改成 per-user，同一个 model="extract" 会随调用 token 的归属解析到
        不同 Agent——所有既有调用方的语义静默改变。
        """
        with _conn(db) as c:
            assert _column(c, "agent", "slug")["notnull"] == 1
            uniques = [r["name"] for r in c.execute("PRAGMA index_list(agent)") if r["unique"] == 1]
            assert uniques, "agent.slug 没有 unique 索引"
            # 行为验证：真的插两行同名
            now = "2026-01-01T00:00:00+00:00"
            c.execute(
                "INSERT INTO agent (slug,name,is_active,user_id,created_at) VALUES (?,?,1,1,?)",
                ("extract", "A", now),
            )
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(
                    "INSERT INTO agent (slug,name,is_active,user_id,created_at) VALUES (?,?,1,1,?)",
                    ("extract", "B", now),
                )

    def test_token_kid_is_unique(self, db: Path):
        """kid 是唯一查表键。撞了就会认错 token。"""
        with _conn(db) as c:
            idx = {r["name"]: r["unique"] for r in c.execute("PRAGMA index_list(token)")}
        assert any(v == 1 for v in idx.values())

    def test_token_carries_hash_alg_and_kdf_params(self, db: Path):
        """换成带盐的 argon2id 时，盐与 t/m/p 参数必须随行走。

        没有这两列的话 scheme=2 上线时又要 ALTER TABLE。
        """
        with _conn(db) as c:
            assert _column(c, "token", "hash_alg")["notnull"] == 1
            assert _column(c, "token", "kdf_params") is not None

    def test_cost_usd_is_text_not_real(self, db: Path):
        """float 存不住 Decimal；且 NULL（无法定价）必须与真实的 0 费用可区分。"""
        with _conn(db) as c:
            assert _column(c, "run_usage", "cost_usd")["type"].upper() == "TEXT"
            assert _column(c, "run_usage", "cost_usd")["notnull"] == 0

    def test_tier_check_reserves_all_four(self, db: Path):
        """v1 只实现 T2，但 CHECK 必须四档全列——否则补 T1 就是一次重建表的迁移。"""
        with _conn(db) as c:
            sql = _table_sql(c, "agent_version")
        for tier in C.Tier:
            assert f"'{tier.value}'" in sql, f"CHECK 里缺 {tier.value}"

    def test_cost_source_check_has_four_states(self, db: Path):
        with _conn(db) as c:
            sql = _table_sql(c, "run_usage")
        for src in C.CostSource:
            assert f"'{src.value}'" in sql, f"CHECK 里缺 {src.value}"

    def test_tier_check_actually_rejects(self, db: Path):
        """CHECK 不只是写在 SQL 里好看。"""
        now = "2026-01-01T00:00:00+00:00"
        with _conn(db) as c:
            c.execute(
                "INSERT INTO agent (id,slug,name,is_active,user_id,created_at) "
                "VALUES (1,'a','A',1,1,?)",
                (now,),
            )
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(
                    "INSERT INTO agent_version "
                    "(agent_id,version,spec_json,tier,user_id,created_at) VALUES (1,1,'{}',?,1,?)",
                    ("T9", now),
                )

    def test_agent_alias_table_exists(self, db: Path):
        """slug 改名的唯一出路。第一次改名时才加表就晚了——那时线上已有调用方。"""
        with _conn(db) as c:
            assert _column(c, "agent_alias", "alias")["notnull"] == 1

    def test_quota_table_exists_though_unenforced(self, db: Path):
        """v1 不执行配额，但表在位，v0.4 加逻辑时不动 schema。"""
        with _conn(db) as c:
            sql = _table_sql(c, "quota")
        assert "day" in sql and "month" in sql and "total" in sql


# =============================================================================
# 备份与恢复
# =============================================================================


class TestBackupRestore:
    def test_backup_is_crash_consistent(self, db: Path, tmp_path: Path):
        """用 VACUUM INTO 而不是 cp：WAL 下直接复制活库不是崩溃一致的，
        -wal 里可能还有未 checkpoint 的事务，拷出来的文件可能根本打不开。"""
        dest = migrate.backup(db, tmp_path / "backups", tag="test")
        assert dest is not None and dest.exists()
        with _conn(dest) as c:
            assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert c.execute("SELECT COUNT(*) FROM user").fetchone()[0] == 1

    def test_restore_roundtrip(self, db: Path, tmp_path: Path):
        now = "2026-01-01T00:00:00+00:00"
        with _conn(db) as c:
            c.execute(
                "INSERT INTO agent (slug,name,is_active,user_id,created_at) VALUES (?,?,1,1,?)",
                ("before-backup", "A", now),
            )
        dest = migrate.backup(db, tmp_path / "backups")
        assert dest is not None

        with _conn(db) as c:
            c.execute("DELETE FROM agent")
        migrate.restore(dest, db)
        with _conn(db) as c:
            slugs = [r["slug"] for r in c.execute("SELECT slug FROM agent")]
        assert slugs == ["before-backup"]

    def test_restore_refuses_corrupt_backup(self, db: Path, tmp_path: Path):
        """别用一个坏文件覆盖好库。"""
        bad = tmp_path / "corrupt.db"
        bad.write_bytes(b"this is not a sqlite file")
        with pytest.raises(Exception):  # noqa: B017 - sqlite3 或 RuntimeError 都算
            migrate.restore(bad, db)

    def test_backup_before_migration(self, tmp_path: Path):
        """真正跑迁移之前先备份——这是唯一的回头路。"""
        p = tmp_path / "x.db"
        bdir = tmp_path / "backups"
        migrate.upgrade_to_head(p, bdir)
        # 空库首次升级时没有可备份的内容，所以此时备份目录应当是空的
        assert not list(bdir.glob("*.db"))
        # 但这次库里有数据了，任何破坏性操作之前必须留下备份
        migrate.downgrade_to(p, "base", bdir)
        assert list(bdir.glob("*.db")), "downgrade 前没有备份"
