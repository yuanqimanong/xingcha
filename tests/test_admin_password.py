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

    def test_short_env_password_takes_effect_and_is_only_flagged(self):
        """短于建议下限**照用**，只在启动时警告一句。

        原先是"一律拒用"，理由是后台能改写上游 base_url、弱密码等于把付费 key
        交出去。放宽是一次明确的决定：拒用的实际后果是"我在 .env 写了一行、重启、
        发现还是要走首次设密"——而这条捷径的全部意义就是省掉那个流程，拒用等于
        把功能废掉。强度交给用户判断，我们只保证他被告知。

        表单那条路径**不放宽**（见 TestEmptyMeansUnset 的最后一条）：环境变量是
        运维写在自己机器上的文件，表单是任何能打开这一页的人在设。
        """
        short = "a" * (MIN_ADMIN_PASSWORD_LEN - 1)
        assert ws.env_password_usable(short) is True
        assert ws.env_password_in_effect(None, short) is True
        assert ws.verify_admin_password(None, short, short) is True
        # 但它必须能被标出来，否则那条警告无从触发
        assert ws.env_password_is_weak(short) is True
        assert ws.env_password_is_weak("a" * MIN_ADMIN_PASSWORD_LEN) is False

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


# =============================================================================
# 空值 = 没设置
# =============================================================================


class TestEmptyMeansUnset:
    """``.env`` 里键在、值空是**最常见**的形态——从 ``.env.example`` 抄过来就是这样。

    它的意思显然是"我还没填"，所以必须与"填了个短的"分开：**空 = 没设置**，
    走首次设密，不抱怨；**非空 = 生效**，不管多短（短的话启动时警告一次）。

    真踩过：一行空值，服务回落到首次设密，而启动日志一个字没说。
    """

    @pytest.mark.parametrize("raw", [None, "", " ", "   ", "\t", "\n", " \t\n "])
    def test_blank_is_treated_as_not_set(self, raw: str | None):
        assert ws.normalize_env_password(raw) == ""
        assert not ws.env_password_usable(raw)
        # 空值不是"弱密码"，是"没设置"——不该有任何抱怨。
        assert not ws.env_password_is_weak(raw)

    def test_blank_falls_back_to_the_first_time_setup_flow(self):
        assert not ws.env_password_in_effect(None, "")
        assert not ws.env_password_in_effect(None, "   ")

    @pytest.mark.parametrize("raw", ["short", "x", "1234567890"])
    def test_short_but_present_takes_effect_and_is_flagged_weak(self, raw: str):
        """短密码**照用**。

        这是一条经过一次决定的放宽：拒用会让"在 .env 写一行"这条捷径失去全部意义
        （用户重启后发现还是要走首次设密）。代价用一条启动警告承担。
        """
        assert ws.env_password_usable(raw)
        assert ws.env_password_in_effect(None, raw)
        assert ws.verify_admin_password(None, raw, raw)
        assert ws.env_password_is_weak(raw), "短密码要能被标出来，否则警告无从触发"

    def test_a_long_env_password_is_not_flagged(self):
        assert not ws.env_password_is_weak("a-very-long-password")

    def test_surrounding_whitespace_is_stripped_on_both_sides_of_the_comparison(self):
        """两头空白去掉，否则你按看到的字符输入永远登不进去。

        判断"设了没"与"校验对不对"必须走**同一个**归一化，否则会出现最难查的状态：
        启动日志说环境变量生效，而没有任何输入能通过校验。
        """
        padded = "  a-very-long-password  "
        assert ws.env_password_usable(padded)
        assert ws.verify_admin_password(None, "a-very-long-password", padded)
        assert not ws.verify_admin_password(None, padded, padded)

    def test_a_blank_value_never_disturbs_a_stored_password(self):
        stored = ws.hash_password("stored-password-x")
        assert ws.verify_admin_password(stored, "stored-password-x", "")
        assert not ws.verify_admin_password(stored, "wrong", "  ")

    def test_the_browser_setup_flow_keeps_its_floor(self):
        """放宽**只针对环境变量**。

        表单是"任何能打开这一页的人"在设，环境变量是运维写在自己机器上的文件里——
        两者的威胁模型不同，所以下限也不同。
        """
        assert MIN_ADMIN_PASSWORD_LEN >= 12
