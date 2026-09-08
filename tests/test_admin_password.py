"""后台密码：环境变量与库里的哈希，谁说了算。

------------------------------------------------------------------------------
规则：先立者为准
------------------------------------------------------------------------------

    库里有密码            → **库赢**，环境变量被忽略
    库里没有 + 环境变量合格 → 用环境变量
    两者都没有            → 首次设密流程

反过来（环境变量总是优先）会引入一条真实的越权路径：任何能往 ``.env`` 写一行的
人——一次误挂的卷、一个共享的部署目录、一个能写文件的漏洞——就能顶掉已经建好的
管理员密码。"先立者为准"让这条路走不通。

代价是"改 .env 却不生效"必须被说出来，所以启动日志、登录页、设置页、
``admin status`` 四处都有说明。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from conftest import FakeUpstream
from xingcha.app import create_app
from xingcha.config import Settings
from xingcha.contract import MIN_ADMIN_PASSWORD_LEN
from xingcha.crypto import Keyring
from xingcha.db import migrate
from xingcha.services import websession as ws

ENV_PW = "env-password-abc123"
DB_PW = "db-password-abc123"


def _fresh(settings: Settings) -> None:
    settings.ensure_data_dir()
    migrate.upgrade_to_head(settings.db_path, settings.backup_dir)
    Keyring.load_or_create(settings.secret_path)


def _client(settings: Settings) -> TestClient:
    # https：会话 cookie 带 secure=True
    return TestClient(create_app(settings), base_url="https://testserver")


def _login(client: TestClient, password: str, confirm: str | None = None):
    data = {"password": password}
    if confirm is not None:
        data["confirm"] = confirm
    return client.post("/admin/login", data=data, follow_redirects=False)


# =============================================================================
# 判定函数本身
# =============================================================================


class TestPriorityRule:
    def test_db_password_wins(self):
        """**库里有密码时环境变量被忽略。**

        这条是整条规则的安全支点：否则能写 .env 就能顶掉管理员密码。
        """
        assert ws.env_password_in_effect("$argon2id$...", ENV_PW) is False

    def test_env_applies_when_db_is_empty(self):
        assert ws.env_password_in_effect(None, ENV_PW) is True

    def test_neither_means_setup_flow(self):
        assert ws.env_password_in_effect(None, None) is False

    def test_short_env_password_is_refused_not_downgraded(self):
        """短于下限**一律拒用**，不是"警告后放过"。

        一个 4 位的后台密码在公网机器上是实打实的洞，而这个功能的意义只是方便。
        """
        assert ws.env_password_usable("a" * (MIN_ADMIN_PASSWORD_LEN - 1)) is False
        assert ws.env_password_usable("a" * MIN_ADMIN_PASSWORD_LEN) is True
        # 拒用之后回落到库/首次设密，用户不会被锁在门外
        assert ws.env_password_in_effect(None, "short") is False

    def test_never_two_valid_passwords(self):
        """绝不"两个都能用"。

        "我改了密码但旧的还能登"是最坏的一种安全体验。
        """
        db_hash = ws.hash_password(DB_PW)
        assert ws.verify_admin_password(db_hash, DB_PW, ENV_PW) is True
        assert ws.verify_admin_password(db_hash, ENV_PW, ENV_PW) is False


# =============================================================================
# 四种组合的端到端行为
# =============================================================================


@pytest.fixture
def env_only(settings: Settings, upstream: FakeUpstream) -> Iterator[TestClient]:
    """库里没密码 + 环境变量设了。"""
    _fresh(settings)
    with _client(Settings(data_dir=settings.data_dir, admin_password=ENV_PW)) as c:
        yield c


class TestEnvOnly:
    def test_login_with_the_env_password(self, env_only: TestClient):
        r = _login(env_only, ENV_PW)
        assert r.status_code == 303, r.text[:200]
        assert env_only.cookies.get("xc_session")

    def test_wrong_password_still_rejected(self, env_only: TestClient):
        assert _login(env_only, "not-the-password-x").status_code != 303

    def test_no_setup_prompt(self, env_only: TestClient):
        """**不该走首次设密。**

        走了的话用户会以为自己设了个新密码，而下次登录仍按环境变量校验——
        一个"我明明改了"却毫无效果的状态。
        """
        body = env_only.get("/admin/login").text
        assert "设置管理员密码" not in body
        assert "环境变量" in body

    def test_ui_change_password_is_refused(self, env_only: TestClient):
        """改了不生效，所以直接拒绝。

        假装成功是最坏的选择：用户以为换了密码，而旧的那个仍然能登。
        """
        _login(env_only, ENV_PW)
        env_only.get("/admin/settings")
        r = env_only.post(
            "/admin/settings/password",
            data={
                "current": ENV_PW,
                "new_password": "another-password-1",
                "confirm": "another-password-1",
                "csrf_token": env_only.cookies.get("xc_csrf") or "",
            },
            follow_redirects=False,
        )
        assert r.status_code == 403
        assert "环境变量" in r.text
        # 旧密码仍然有效——说明真的没改
        env_only.cookies.clear()
        assert _login(env_only, ENV_PW).status_code == 303

    def test_settings_page_explains(self, env_only: TestClient):
        _login(env_only, ENV_PW)
        body = env_only.get("/admin/settings").text
        assert "环境变量托管" in body
        assert ENV_PW not in body, "页面绝不能回显密码"


class TestDbWins:
    def test_env_is_ignored_once_the_db_has_a_password(
        self, settings: Settings, upstream: FakeUpstream
    ):
        """先在后台设密，**之后**再配环境变量——环境变量不生效。

        这正是用户要的语义，也是防越权的那一条。
        """
        _fresh(settings)
        # 第一步：没有环境变量，走首次设密
        with _client(settings) as c:
            assert _login(c, DB_PW, confirm=DB_PW).status_code == 303

        # 第二步：加上环境变量重启
        with _client(Settings(data_dir=settings.data_dir, admin_password=ENV_PW)) as c:
            assert _login(c, ENV_PW).status_code != 303, "环境变量竟然能登进去"
            assert _login(c, DB_PW).status_code == 303, "库里的密码应当仍然有效"

    def test_settings_page_says_it_is_ignored(self, settings: Settings, upstream: FakeUpstream):
        """必须说出来。

        不说的话：用户改了 .env、重启、发现没变化，而根因（库里已有密码）看不见。
        """
        _fresh(settings)
        with _client(settings) as c:
            _login(c, DB_PW, confirm=DB_PW)
        with _client(Settings(data_dir=settings.data_dir, admin_password=ENV_PW)) as c:
            _login(c, DB_PW)
            body = c.get("/admin/settings").text
        assert "被忽略" in body
        assert "reset-password" in body, "要告诉用户怎么改用环境变量"

    def test_ui_change_password_still_works(self, settings: Settings, upstream: FakeUpstream):
        """库赢的时候后台改密码照常可用。"""
        _fresh(settings)
        with _client(settings) as c:
            _login(c, DB_PW, confirm=DB_PW)
            c.get("/admin/settings")
            r = c.post(
                "/admin/settings/password",
                data={
                    "current": DB_PW,
                    "new_password": "brand-new-password-1",
                    "confirm": "brand-new-password-1",
                    "csrf_token": c.cookies.get("xc_csrf") or "",
                },
                follow_redirects=False,
            )
            assert r.status_code == 303, r.text[:200]
            c.cookies.clear()
            assert _login(c, DB_PW).status_code != 303, "旧密码还能登"
            assert _login(c, "brand-new-password-1").status_code == 303


class TestResetIsBlockedUnderEnv:
    def _reset(self, settings: Settings):
        from typer.testing import CliRunner

        from xingcha import config as config_mod
        from xingcha.cli import app

        config_mod.reset_settings()
        env = {"XINGCHA_DATA_DIR": str(settings.data_dir)}
        if settings.admin_password:
            env["XINGCHA_ADMIN_PASSWORD"] = settings.admin_password
        try:
            return CliRunner(env=env).invoke(app, ["admin", "reset-password", "--yes"])
        finally:
            config_mod.reset_settings()

    def test_refused_when_env_is_in_effect(self, settings: Settings):
        """环境变量生效时重置**没有意义**，所以拒绝。

        这时库里本来就没有密码（那正是生效条件），清空是个空操作；而它会让人以为
        "重置了、可以重新设一个"——实际下次登录仍按环境变量校验。
        """
        _fresh(settings)
        r = self._reset(Settings(data_dir=settings.data_dir, admin_password=ENV_PW))
        assert r.exit_code != 0
        assert "环境变量" in r.output
        assert ".env" in r.output, "要告诉用户去哪儿改"

    def test_allowed_when_the_db_owns_the_password(
        self, settings: Settings, upstream: FakeUpstream
    ):
        """库赢的时候重置照常可用——那正是"改用环境变量"的入口。"""
        _fresh(settings)
        with _client(settings) as c:
            _login(c, DB_PW, confirm=DB_PW)
        r = self._reset(Settings(data_dir=settings.data_dir, admin_password=ENV_PW))
        assert r.exit_code == 0, r.output

        # 重置之后库里空了 → 环境变量接管
        with _client(Settings(data_dir=settings.data_dir, admin_password=ENV_PW)) as c:
            assert _login(c, ENV_PW).status_code == 303


class TestNeverLeaks:
    def test_password_never_appears_in_logs(self, settings: Settings, caplog):
        """启动日志会说"由环境变量托管"，但**绝不能带上那个值**。"""
        import logging

        _fresh(settings)
        with (
            caplog.at_level(logging.DEBUG),
            _client(Settings(data_dir=settings.data_dir, admin_password=ENV_PW)),
        ):
            pass
        text = caplog.text
        assert "XINGCHA_ADMIN_PASSWORD" in text, "该说的没说"
        assert ENV_PW not in text, "日志里泄漏了密码"

    def test_login_page_never_shows_it(self, env_only: TestClient):
        assert ENV_PW not in env_only.get("/admin/login").text
