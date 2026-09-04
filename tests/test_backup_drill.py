"""备份恢复演练。

------------------------------------------------------------------------------
为什么这一层非要真删
------------------------------------------------------------------------------

开发计划 §4 的 A10 写着「**备份不可信**」，并把「必须做一次真的删掉 data 再恢复
并跑通全部验收的演练」列成交付项。那条风险的要点不是"忘记备份"，而是三件更隐蔽的事：

1. WAL 模式下 ``cp`` 活库**不是崩溃一致的**——所以备份必须走 ``VACUUM INTO``；
2. 备份**不含密钥环**，只恢复数据库会得到一库永久解不开的密文；
3. "备份文件在那儿"不等于"备份能用"——灾难当天才发现它是坏的，等于从来没备份过。

只测 ``backup()`` 返回了一个路径，这三件事一件都没测到。所以这里真的
``rmtree(data_dir)``，然后从备份重建，再把验收断言重跑一遍。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import FakeUpstream
from xingcha import contract as C
from xingcha.app import create_app
from xingcha.config import Settings
from xingcha.contract import Tier
from xingcha.crypto import Keyring, KeyringMissing
from xingcha.db import migrate
from xingcha.db.engine import make_engine, make_sessionmaker
from xingcha.services import agent as agent_svc
from xingcha.services import auth as auth_svc
from xingcha.services import quota as quota_svc
from xingcha.services import setting as setting_svc

SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}},
    "required": ["title"],
}
GOOD = {"title": "还在"}


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def seed(settings: Settings, base_url: str) -> str:
    """把一个"有真实内容"的系统建起来：配置、Agent、令牌、配额。"""
    settings.ensure_data_dir()
    migrate.upgrade_to_head(settings.db_path, settings.backup_dir)
    keyring = Keyring.load_or_create(settings.secret_path)

    async def go() -> str:
        engine = make_engine(settings.db_path)
        maker = make_sessionmaker(engine)
        async with maker() as s:
            await setting_svc.set_(s, keyring, C.SETTING_KEY_OPENROUTER_API_KEY, "sk-or-v1-secret")
            await setting_svc.set_(s, keyring, C.SETTING_KEY_OPENROUTER_BASE_URL, base_url)
            await agent_svc.save(
                s,
                slug="survivor",
                name="活下来的",
                description="恢复之后还得能跑",
                instructions="抽取标题。",
                model="openai/gpt-5",
                schema_text=json.dumps(SCHEMA),
                requested_tier=Tier.T2,
                capabilities=None,
                retries=2,
                native_ok=True,
            )
            await quota_svc.upsert(
                s,
                subject_type="user",
                subject_id=1,
                window="day",
                limit_usd=Decimal("5"),
                limit_requests=None,
            )
            tok = await auth_svc.issue(s, name="演练")
            await s.commit()
        await engine.dispose()
        return tok.plaintext

    return asyncio.run(go())


def acceptance(settings: Settings, token: str, upstream: FakeUpstream) -> None:
    """恢复之后要重跑的验收。

    逐条对应一件恢复可能悄悄搞坏的事：

    - 令牌还认吗 → token 表与它的 hash 完整
    - 上游 key 解得开吗 → 密钥环与密文对得上（这条是 A10 的核心）
    - Agent 还在吗、还能跑吗 → agent 表与 out_schema 完整
    - 配额还在吗 → quota 表完整，且启动时被重新加载
    """
    upstream.tool_payloads = [GOOD]
    app = create_app(settings)
    with TestClient(app) as client:
        models = client.get("/v1/models", headers=auth(token))
        assert models.status_code == 200, "令牌恢复后不认了"
        assert "survivor" in {m["id"] for m in models.json()["data"]}, "Agent 没恢复"

        r = client.post(
            "/v1/chat/completions",
            json={"model": "survivor", "messages": [{"role": "user", "content": "x"}]},
            headers=auth(token),
        )
        assert r.status_code == 200, f"恢复后跑不通：{r.text[:300]}"
        assert json.loads(r.json()["choices"][0]["message"]["content"]) == GOOD

        assert app.state.xc.quota is not None
        assert app.state.xc.quota.rule_count == 1, "配额规则没恢复"


# =============================================================================
# 完整演练
# =============================================================================


def test_full_drill_wipe_and_restore(tmp_path: Path, upstream: FakeUpstream):
    """**真的删掉 data/，再从备份重建，验收全过。**

    这是 A10 唯一的证明。中间那一步是 ``shutil.rmtree`` 而不是"改个路径"——
    改路径的演练测不出"恢复流程漏了某个文件"，而那正是最常见的失败。
    """
    settings = Settings(data_dir=tmp_path / "data", request_timeout=10.0, catalog_ttl_seconds=60)
    token = seed(settings, upstream.base_url)
    upstream.reset()

    # 演练前先确认这套东西本来是好的，否则后面"恢复失败"分不清是谁的锅
    acceptance(settings, token, upstream)
    upstream.reset()

    # ---- 备份：库走 VACUUM INTO，密钥环单独拷 ----
    vault = tmp_path / "vault"
    vault.mkdir()
    db_backup = migrate.backup(settings.db_path, settings.backup_dir, tag="drill")
    assert db_backup is not None
    shutil.copy2(db_backup, vault / "xingcha.db")
    shutil.copy2(settings.secret_path, vault / "secret.key")

    # ---- 灾难 ----
    shutil.rmtree(settings.data_dir)
    assert not settings.data_dir.exists()

    # ---- 恢复 ----
    settings.ensure_data_dir()
    shutil.copy2(vault / "secret.key", settings.secret_path)
    migrate.restore(vault / "xingcha.db", settings.db_path)

    # ---- 验收 ----
    acceptance(settings, token, upstream)


def test_restoring_without_the_keyring_refuses_loudly(tmp_path: Path, upstream: FakeUpstream):
    """只恢复数据库、忘了密钥环 → **拒绝启动**，而不是起来之后处处报怪错。

    这是备份策略里最容易犯的错：``data/backups/`` 里躺着一堆库文件，看着很齐全，
    而解开它们的那把钥匙从来没被备份过。

    静默重新生成一把新密钥环是最坏的结果：服务能启动、后台能登录、Agent 列表也
    在，只有真正调用上游时才失败——而那时你已经把"恢复成功"报出去了，而且旧密文
    已经**永久**解不开。
    """
    settings = Settings(data_dir=tmp_path / "data", request_timeout=10.0, catalog_ttl_seconds=60)
    seed(settings, upstream.base_url)

    vault = tmp_path / "vault"
    vault.mkdir()
    db_backup = migrate.backup(settings.db_path, settings.backup_dir, tag="nokey")
    assert db_backup is not None
    shutil.copy2(db_backup, vault / "xingcha.db")
    # **故意不备份 secret.key**

    shutil.rmtree(settings.data_dir)
    settings.ensure_data_dir()
    migrate.restore(vault / "xingcha.db", settings.db_path)

    with pytest.raises(KeyringMissing) as e, TestClient(create_app(settings)):
        pass

    message = str(e.value)
    assert "拒绝启动" in message
    assert "从备份恢复" in message, "报错必须说清该怎么做，不能只说失败了"
    assert not settings.secret_path.exists(), "**绝不能**顺手生成一把新的"


def test_a_fresh_install_still_creates_a_keyring(tmp_path: Path):
    """反向的那一半：库里没有密文时，新建密钥环是正常的初始化路径。

    上面那条守卫要是收得太紧，第一次部署就跑不起来了。两条一起才把边界钉住。
    """
    settings = Settings(data_dir=tmp_path / "data")
    settings.ensure_data_dir()
    migrate.upgrade_to_head(settings.db_path, settings.backup_dir)

    from xingcha.bootstrap import prepare

    keyring = prepare(settings)
    assert keyring is not None
    assert settings.secret_path.exists()


# =============================================================================
# 备份体检
# =============================================================================


class TestVerifyBackup:
    def test_reports_contents_of_a_good_backup(self, tmp_path: Path, upstream: FakeUpstream):
        settings = Settings(data_dir=tmp_path / "data")
        seed(settings, upstream.base_url)
        path = migrate.backup(settings.db_path, settings.backup_dir)
        assert path is not None

        report = migrate.verify_backup(path)
        assert report.usable
        assert report.integrity_ok
        assert report.revision
        assert report.counts["agent"] == 1
        assert report.counts["token"] == 1
        assert report.counts["quota"] == 1
        assert report.ciphertext_rows >= 1, "上游 key 是密文，必须被数出来"

    def test_missing_file_is_a_problem_not_a_crash(self, tmp_path: Path):
        report = migrate.verify_backup(tmp_path / "nope.db")
        assert not report.usable
        assert "不存在" in report.problems[0]

    def test_corrupted_backup_is_caught(self, tmp_path: Path, upstream: FakeUpstream):
        """一个被截断的备份必须在**恢复之前**被认出来。

        不认出来的话，``restore`` 会用一个坏文件盖掉还活着的库——一次本可挽回的
        事故变成不可挽回的。
        """
        settings = Settings(data_dir=tmp_path / "data")
        seed(settings, upstream.base_url)
        path = migrate.backup(settings.db_path, settings.backup_dir)
        assert path is not None

        raw = path.read_bytes()
        path.write_bytes(raw[: len(raw) // 2])  # 砍掉一半

        report = migrate.verify_backup(path)
        assert not report.usable
        assert report.problems

        with pytest.raises((RuntimeError, sqlite3.DatabaseError)):
            migrate.restore(path, settings.db_path)

    def test_not_a_xingcha_db_is_a_problem(self, tmp_path: Path):
        """别的 sqlite 库也能通过完整性检查——所以还得看 alembic_version。"""
        stranger = tmp_path / "stranger.db"
        with sqlite3.connect(stranger) as c:
            c.execute("CREATE TABLE whatever (a INTEGER)")
        report = migrate.verify_backup(stranger)
        assert not report.usable
        assert any("星槎" in p for p in report.problems)


# =============================================================================
# drill.sh 依赖的 CLI 契约
# =============================================================================


class TestCliContractUsedByDrill:
    """``deploy/drill.sh`` 靠这几条命令与退出码工作。

    改名或改退出码不会让任何单元测试变红，但会让演练脚本在**真出事那天**才失败——
    而那正是最不能失败的时刻。所以把它当契约钉住。
    """

    def _runner(self, settings: Settings):
        from typer.testing import CliRunner

        from xingcha import config

        config.reset_settings()
        return CliRunner(env={"XINGCHA_DATA_DIR": str(settings.data_dir)})

    def _invoke(self, settings: Settings, *args: str):
        from xingcha import config
        from xingcha.cli import app

        runner = self._runner(settings)
        try:
            return runner.invoke(app, list(args))
        finally:
            config.reset_settings()

    def test_verify_exits_zero_on_a_good_backup(self, tmp_path: Path, upstream: FakeUpstream):
        settings = Settings(data_dir=tmp_path / "data")
        seed(settings, upstream.base_url)
        assert migrate.backup(settings.db_path, settings.backup_dir) is not None

        result = self._invoke(settings, "db", "verify")
        assert result.exit_code == 0, result.output
        assert "这份备份可用" in result.output

    def test_verify_warns_that_the_keyring_is_not_inside(
        self, tmp_path: Path, upstream: FakeUpstream
    ):
        """体检报告必须点出"密钥环不在这个文件里"。

        不点出来的话，一份"体检通过"的备份会给人完整的错觉——而它恰恰是不完整的。
        """
        settings = Settings(data_dir=tmp_path / "data")
        seed(settings, upstream.base_url)
        migrate.backup(settings.db_path, settings.backup_dir)

        result = self._invoke(settings, "db", "verify")
        assert "密钥环" in result.output
        assert "secret.key" in result.output

    def test_verify_exits_nonzero_on_a_bad_backup(self, tmp_path: Path, upstream: FakeUpstream):
        """``drill.sh`` 用 `|| die` 判这一步，所以退出码必须非零。"""
        settings = Settings(data_dir=tmp_path / "data")
        seed(settings, upstream.base_url)
        path = migrate.backup(settings.db_path, settings.backup_dir)
        assert path is not None
        raw = path.read_bytes()
        path.write_bytes(raw[: len(raw) // 3])

        result = self._invoke(settings, "db", "verify")
        assert result.exit_code != 0

    def test_verify_says_what_to_do_when_there_is_no_backup(self, tmp_path: Path):
        settings = Settings(data_dir=tmp_path / "data")
        settings.ensure_data_dir()
        result = self._invoke(settings, "db", "verify")
        assert result.exit_code != 0
        assert "db backup" in result.output, "报错要给出下一步命令，不能只说没有"
