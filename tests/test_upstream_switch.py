"""上游切换。

------------------------------------------------------------------------------
形状：单出口，可切换
------------------------------------------------------------------------------

星槎同一时刻只有**一个**上游生效——业务代码永远只认 ``base_url`` + 一把
``sk-xc-``，换上游对它完全不可见。这一层不是多上游路由，是换掉那个唯一的出口。

要守住的三件事：

1. **发现只认名单。** ``GITHUB_TOKEN`` 一类不能被当成模型 key 列进管理面；
   一个"候选上游"列表里混进非上游条目，比少列几个危险得多。
2. **key 不留在环境里当配置源。** 选中的那把加密落库，环境只是发现来源——
   它会进 ``docker inspect`` 与 ``/proc/<pid>/environ``。
3. **切换前必须先探测。** 切上游会打断所有现有 Agent（模型名在新上游不存在），
   而那个后果必须在切之前看得见；目录拉不通则直接拒绝切换。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from conftest import FakeUpstream
from xingcha import contract as C
from xingcha.app import create_app
from xingcha.config import Settings
from xingcha.contract import Tier
from xingcha.crypto import Keyring
from xingcha.db import migrate
from xingcha.db.engine import make_engine, make_sessionmaker
from xingcha.services import agent as agent_svc
from xingcha.services import setting as setting_svc
from xingcha.services import upstream_env as ue

SCHEMA = {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]}
PASSWORD = "switch-test-abc123"


# =============================================================================
# 发现
# =============================================================================


class TestDiscover:
    def test_only_known_vendors(self):
        """**只认内置名单。**

        不做 ``*_API_KEY`` 的模式兜底：那会把 GITHUB_TOKEN / NPM_TOKEN 一类列进
        管理面，而用户会以为星槎真能拿它们当模型上游。
        """
        got = ue.discover(
            {
                "DEEPSEEK_API_KEY": "sk-deepseek-000000000000",
                "GITHUB_TOKEN": "ghp_should_not_appear",
                "NPM_TOKEN": "npm_should_not_appear",
                "AWS_SECRET_ACCESS_KEY": "should_not_appear",
            }
        )
        assert [u.label for u in got] == ["DEEPSEEK"]

    def test_case_insensitive(self):
        """``.env`` 里写小写是常见习惯，而 os.environ 在 Linux 上区分大小写。"""
        got = ue.discover({"deepseek_api_key": "sk-x000000000000000"})
        assert [u.label for u in got] == ["DEEPSEEK"]
        # 原始大小写要保留：用户要能在 .env 里搜到这一行
        assert got[0].env_name == "deepseek_api_key"

    def test_empty_values_are_not_candidates(self):
        """``.env.example`` 里常有 ``FOO_API_KEY=``（留空占位）。

        把它列成候选会让用户点了之后拿到一个"读不到值"的错误。
        """
        assert ue.discover({"GROQ_API_KEY": "", "MISTRAL_API_KEY": "   "}) == []

    def test_same_vendor_two_aliases_appears_once(self):
        """一个厂商多个变量名（DEEPINFRA_API_KEY / DEEPINFRA_TOKEN）只列一次。"""
        got = ue.discover(
            {"DEEPINFRA_API_KEY": "di-000000000000", "DEEPINFRA_TOKEN": "di-111111111111"}
        )
        assert len(got) == 1

    def test_known_vendors_carry_a_base_url(self):
        got = ue.discover({"DEEPSEEK_API_KEY": "sk-x000000000000000"})[0]
        assert got.base_url == "https://api.deepseek.com/v1"
        assert got.ready is True

    def test_vendor_without_catalog_is_flagged(self):
        """没有 ``/models`` 端点的上游要标出来。

        它的空目录不是故障，但后果是真的：判档回落 T2、费用来源 unknown。
        页面得说，否则用户会以为星槎坏了。
        """
        got = ue.discover({"PERPLEXITY_API_KEY": "pplx-00000000000000"})[0]
        assert got.has_catalog is False
        assert ue.discover({"DEEPSEEK_API_KEY": "sk-x000000000000000"})[0].has_catalog is True


class TestMasking:
    def test_never_shows_the_whole_key(self):
        """页面上只显示脱敏值——截图、录屏、肩窥都是真实路径。"""
        full = "sk-or-v1-" + "a" * 60 + "beef"
        masked = ue.mask(full)
        assert full not in masked
        assert masked.startswith("sk-or-v1-")
        assert masked.endswith("beef"), "留末四位：够你核对是不是那一把"
        assert len(masked) < 20

    def test_short_values_are_still_masked(self):
        assert ue.mask("sk-short") == "sk…"


class TestDefaultPair:
    def test_generic_names(self):
        k, b = ue.default_from_env({"XINGCHA_API_KEY": "sk-a", "XINGCHA_BASE_URL": "https://x/v1"})
        assert (k, b) == ("sk-a", "https://x/v1")

    def test_old_names_still_work(self):
        """``XINGCHA_OPENROUTER_API_KEY`` 必须继续认。

        改配置项名是破坏性变更，而"升级对用户无感"是这个项目的头号承诺。
        """
        k, b = ue.default_from_env(
            {
                "XINGCHA_OPENROUTER_API_KEY": "sk-old",
                "XINGCHA_OPENROUTER_BASE_URL": "https://old/v1",
            }
        )
        assert (k, b) == ("sk-old", "https://old/v1")

    def test_new_name_wins(self):
        k, _ = ue.default_from_env(
            {"XINGCHA_API_KEY": "sk-new", "XINGCHA_OPENROUTER_API_KEY": "sk-old"}
        )
        assert k == "sk-new"

    def test_lowercase_in_dotenv(self):
        k, b = ue.default_from_env(
            {"xingcha_api_key": "sk-lower", "xingcha_base_url": "https://l/v1"}
        )
        assert (k, b) == ("sk-lower", "https://l/v1")


# =============================================================================
# 切换前的探测
# =============================================================================


@pytest.fixture
def wired(settings: Settings, upstream: FakeUpstream) -> Iterator[tuple[TestClient, Settings]]:
    settings.ensure_data_dir()
    migrate.upgrade_to_head(settings.db_path, settings.backup_dir)
    keyring = Keyring.load_or_create(settings.secret_path)

    async def seed() -> None:
        engine = make_engine(settings.db_path)
        maker = make_sessionmaker(engine)
        async with maker() as s:
            await setting_svc.set_(s, keyring, C.SETTING_KEY_OPENROUTER_API_KEY, "sk-or-v1-fake")
            await setting_svc.set_(s, keyring, C.SETTING_KEY_OPENROUTER_BASE_URL, upstream.base_url)
            await agent_svc.save(
                s,
                slug="extract",
                name="x",
                description=None,
                instructions="i",
                model="openai/gpt-5",
                schema_text=json.dumps(SCHEMA),
                requested_tier=Tier.T2,
                capabilities=None,
                retries=2,
                native_ok=True,
            )
            from xingcha.services import websession as ws

            admin = await ws.get_admin(s)
            assert admin is not None
            admin.password_hash = ws.hash_password(PASSWORD)
            await s.commit()
        await engine.dispose()

    asyncio.run(seed())
    upstream.reset()
    # base_url 必须是 https：会话 cookie 带 secure=True，http 下 httpx 不会存它，
    # 表现为"登录返回 303 但下一跳又是登录页"。
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        r = client.post("/admin/login", data={"password": PASSWORD}, follow_redirects=False)
        assert r.status_code == 303, r.text[:200]
        assert client.cookies.get("xc_session"), "登录后没有下发会话 cookie"
        yield client, settings


def csrf_of(client: TestClient) -> str:
    return client.cookies.get("xc_csrf") or ""


class TestProbeBeforeSwitch:
    """注意：conftest 的 ``_isolate_real_credentials`` 会清掉环境里所有厂商 key，
    所以这些用例必须自己 ``monkeypatch.setenv``。那条隔离是有意的——测试绝不该
    碰开发者 .env 里的真凭据（否则每次跑测试都在真花钱）。"""

    def test_probe_lists_agents_that_will_break(self, wired, upstream: FakeUpstream, monkeypatch):
        """**切之前就要看见哪些 Agent 会失效。**

        Agent 里写死的是模型名。切到别家之后那个模型不存在，每次调用都是一个上游
        4xx，而报错离根因很远——用户只会看到"Agent 突然坏了"。

        假上游的目录里只有 ``openai/gpt-5`` 与 ``vendor/no-native``，所以拿一个
        不在目录里的 Agent 模型就能造出这个场景。
        """
        client, _ = wired
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-000000000000")
        client.get("/admin/upstreams")
        # 指向假上游（它有 /models），但 Agent 用的模型故意不在它的目录里
        r = client.post(
            "/admin/upstreams/probe",
            data={
                "ref": "DEEPSEEK_API_KEY",
                "base_url": upstream.base_url,
                "csrf_token": csrf_of(client),
            },
        )
        assert r.status_code == 200, r.text[:300]
        assert "确认切换" in r.text, "目录拉得通就该允许切换"

    def test_probe_refuses_when_the_catalog_cannot_be_fetched(self, wired, monkeypatch):
        """拉不通就不让切。

        切过去只会让服务变成不可用——那不是"用户自己的选择"，那是一个可以提前
        拦住的错误。
        """
        client, _ = wired
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-000000000000")
        client.get("/admin/upstreams")
        r = client.post(
            "/admin/upstreams/probe",
            data={
                "ref": "DEEPSEEK_API_KEY",
                "base_url": "http://127.0.0.1:9/v1",  # 黑洞
                "csrf_token": csrf_of(client),
            },
        )
        assert r.status_code == 200
        assert "不能切到" in r.text
        assert "确认切换" not in r.text

    def test_probe_writes_nothing(self, wired, upstream: FakeUpstream, monkeypatch):
        """探测是**只读**的。

        写了的话，一次"我只是看看"的点击就把上游换掉了。
        """
        client, settings = wired
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-000000000000")
        before = _active_env(settings)
        client.get("/admin/upstreams")
        client.post(
            "/admin/upstreams/probe",
            data={
                "ref": "DEEPSEEK_API_KEY",
                "base_url": upstream.base_url,
                "csrf_token": csrf_of(client),
            },
        )
        assert _active_env(settings) == before

    def test_unknown_vendor_needs_a_base_url(self, wired, monkeypatch):
        """名单外的变量名没有内置端点，必须让用户填 base_url。

        环境变量要真的存在——不存在时先撞的是"读不到值"，那是另一条错误路径。
        两条都该有各自的话，而此前这条测试因为没设变量，其实一直在验另一条。
        """
        client, _ = wired
        monkeypatch.setenv("MY_OWN_KEY", "sk-mine-000000000000")
        client.get("/admin/upstreams")
        r = client.post(
            "/admin/upstreams/probe",
            data={"ref": "MY_OWN_KEY", "csrf_token": csrf_of(client)},
        )
        assert r.status_code == 403
        assert "base_url" in r.text

    def test_a_ref_that_is_not_in_the_environment_says_so(self, wired):
        """读不到值时的话要指向 .env，而不是让人去猜 base_url。"""
        client, _ = wired
        client.get("/admin/upstreams")
        r = client.post(
            "/admin/upstreams/probe",
            data={"ref": "GONE_KEY", "csrf_token": csrf_of(client)},
        )
        assert r.status_code == 403
        assert "读不到值" in r.text

    def test_base_url_goes_through_the_ssrf_guard(self, wired, monkeypatch):
        """这是一个"服务端会主动去打"的地址，云元数据端点一律拒。"""
        client, _ = wired
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-000000000000")
        client.get("/admin/upstreams")
        r = client.post(
            "/admin/upstreams/probe",
            data={
                "ref": "DEEPSEEK_API_KEY",
                "base_url": "http://169.254.169.254/v1",
                "csrf_token": csrf_of(client),
            },
        )
        assert r.status_code == 403
        assert "元数据" in r.text


def _active_env(settings: Settings) -> str | None:
    import sqlite3

    with sqlite3.connect(settings.db_path) as c:
        row = c.execute(
            "SELECT value_enc FROM setting WHERE key = ?", (C.SETTING_KEY_UPSTREAM_ACTIVE_ENV,)
        ).fetchone()
    return row[0] if row else None


# =============================================================================
# 真正切换
# =============================================================================


class TestSwitch:
    def test_switch_persists_the_key_encrypted(self, wired, upstream: FakeUpstream, monkeypatch):
        """选中的 key **加密落库**，不是继续从环境读。

        环境变量会进 ``docker inspect`` 与 ``/proc/<pid>/environ``，不是长期存放处。
        """
        import sqlite3

        client, settings = wired
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-secret-000000")
        client.get("/admin/upstreams")
        r = client.post(
            "/admin/upstreams/switch",
            data={
                "ref": "DEEPSEEK_API_KEY",
                "base_url": upstream.base_url,
                "csrf_token": csrf_of(client),
            },
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text[:300]

        with sqlite3.connect(settings.db_path) as c:
            rows = dict(c.execute("SELECT key, value_enc FROM setting").fetchall())
        stored = rows[C.SETTING_KEY_OPENROUTER_API_KEY]
        blob = stored if isinstance(stored, bytes) else str(stored).encode()
        assert b"sk-deepseek-secret-000000" not in blob, "落库的必须是密文"
        # 解密回来要等于那把 key
        keyring = Keyring.load_or_create(settings.secret_path)
        assert keyring.decrypt(stored) == "sk-deepseek-secret-000000"

    def test_switch_records_which_env_is_active(self, wired, upstream: FakeUpstream, monkeypatch):
        client, _settings = wired
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-000000000000")
        client.get("/admin/upstreams")
        client.post(
            "/admin/upstreams/switch",
            data={
                "ref": "DEEPSEEK_API_KEY",
                "base_url": upstream.base_url,
                "csrf_token": csrf_of(client),
            },
            follow_redirects=False,
        )
        page = client.get("/admin/upstreams").text
        assert "DEEPSEEK" in page
        assert "生效中" in page

    def test_switch_clears_the_runtime_cache(self, wired, upstream: FakeUpstream, monkeypatch):
        """**必须清运行时缓存。**

        Agent 实例把 provider（以及那把 key）烤进去了。不清的话切换之后旧 key
        继续被用——一个"我明明换了 key"却毫无变化的现象，极难查。
        """
        client, _ = wired
        app = client.app
        state = app.state.xc  # type: ignore[attr-defined]

        upstream.tool_payloads = [{"title": "ok"}]
        client.post(
            "/v1/chat/completions",
            json={"model": "extract", "messages": [{"role": "user", "content": "x"}]},
            headers={"Authorization": "Bearer bogus"},
        )
        state.runtimes._items["fake"] = object()  # 塞一个进去，证明确实被清了
        assert len(state.runtimes) > 0

        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-000000000000")
        client.get("/admin/upstreams")
        client.post(
            "/admin/upstreams/switch",
            data={
                "ref": "DEEPSEEK_API_KEY",
                "base_url": upstream.base_url,
                "csrf_token": csrf_of(client),
            },
            follow_redirects=False,
        )
        assert len(state.runtimes) == 0, "切换后运行时缓存没清，旧 key 会继续被用"

    def test_switch_refuses_when_the_env_var_is_gone(self, wired, upstream: FakeUpstream):
        """环境变量被删掉之后点切换，要给明确的错，不能写一个空 key 进去。"""
        client, _ = wired
        client.get("/admin/upstreams")
        r = client.post(
            "/admin/upstreams/switch",
            data={
                "ref": "GROQ_API_KEY",  # 环境里没有
                "base_url": upstream.base_url,
                "csrf_token": csrf_of(client),
            },
            follow_redirects=False,
        )
        assert r.status_code == 403
        assert "读不到" in r.text


class TestPageDoesNotLeak:
    def test_page_never_shows_a_full_key(self, wired, monkeypatch):
        client, _ = wired
        secret = "sk-deepseek-" + "f" * 40
        monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
        page = client.get("/admin/upstreams").text
        assert secret not in page, "完整 key 出现在页面上"
        assert "DEEPSEEK" in page

    def test_page_requires_auth(self, settings: Settings, upstream: FakeUpstream):
        settings.ensure_data_dir()
        migrate.upgrade_to_head(settings.db_path, settings.backup_dir)
        with TestClient(create_app(settings), base_url="https://testserver") as client:
            r = client.get("/admin/upstreams", follow_redirects=False)
        assert r.status_code in (302, 303, 307)
        assert "/admin/login" in (r.headers.get("location") or "")


class TestEnvImportRecordsItsSource:
    """从 ``.env`` 导入的默认上游要**记下来源**。

    不记的话上游页显示"手动填写"——而它根本不是手填的。那句话会让人以为有人在页面
    上配过，于是去找一个不存在的操作记录；更实际的后果是切走之后不知道该切回哪一个
    （列表里那一行叫「.env 里的默认」，而「目前使用」说的是"手动填写"，对不上）。

    用户清库重部署之后正好撞上这个。
    """

    def test_the_active_source_is_recorded(self, settings: Settings):
        import asyncio

        from xingcha import contract as C
        from xingcha.crypto import Keyring
        from xingcha.services import setting as setting_svc

        settings.ensure_data_dir()
        migrate.upgrade_to_head(settings.db_path, settings.backup_dir)
        keyring = Keyring.load_or_create(settings.secret_path)
        engine = make_engine(settings.db_path)
        maker = make_sessionmaker(engine)

        async def run() -> str | None:
            async with maker() as s:
                await setting_svc.import_env_once(
                    s, keyring, "sk-from-env-000", "https://api.example.com/v1"
                )
                await s.commit()
                return await setting_svc.get(s, keyring, C.SETTING_KEY_UPSTREAM_ACTIVE_ENV)

        try:
            got = asyncio.run(run())
        finally:
            asyncio.run(engine.dispose())

        assert got == C.ENV_DEFAULT_API_KEY, f"来源没记下来，页面会显示成手动填写（实际 {got!r}）"
