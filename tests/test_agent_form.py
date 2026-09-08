"""Agent 表单：模型参数、能力、按 Agent 的可观测。

------------------------------------------------------------------------------
这一层守什么
------------------------------------------------------------------------------

表单字段清单**由官方 schema 驱动**，不是手抄的。手抄的清单会随 pydantic-ai 升级
过时，而过时的症状极其隐蔽：``AgentSpec`` 是 ``extra='ignore'``，一个改了名的字段
会被**静默丢掉**——你填了 temperature、存进 spec、跑的却是默认值。

三件具体的事：

1. **留空 ≠ 0。** 表单里留空的意思是"不设这一项、用上游默认"，而
   ``temperature: 0`` 是一条截然不同的指令。混同会把每个 Agent 都变成确定性输出。
2. **编辑要能反填。** 反填不了的话，"我只是想改一句提示词"会把之前设过的参数
   悄悄清掉。
3. **可观测是按 Agent 的。** 全局开关的语义不对：一个跑客户合同的 Agent 与一个
   跑内部分类的 Agent，对"对话内容能不能离开这台机器"的答案通常不同。
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from conftest import FakeUpstream
from xingcha import contract as C
from xingcha.app import create_app
from xingcha.config import Settings
from xingcha.core import builder
from xingcha.crypto import Keyring
from xingcha.db import migrate
from xingcha.db.engine import make_engine, make_sessionmaker
from xingcha.services import setting as setting_svc

PASSWORD = "form-test-abc123456"


class TestFieldListsAreSchemaDriven:
    """字段清单必须对着官方 schema 校验。

    ``AgentSpec`` 是 ``extra='ignore'``：一个改了名的字段会被吞掉、不报错。
    所以过时的症状是"填了没生效"，而不是一个错误——必须在构建期就红。
    """

    def test_every_form_setting_exists_in_the_official_schema(self):
        known = set(builder._spec_schema()["$defs"]["ModelSettings"]["properties"])
        for field, _, _ in builder.model_settings_fields():
            assert field in known, f"{field} 不在官方 ModelSettings 里"

    def test_every_form_capability_exists_in_pydantic_ai(self):
        known = set(builder.declarable_capabilities())
        for name, _, _ in builder.form_capabilities():
            assert name in known, f"{name} 不是官方能力"
        assert builder.CAPABILITY_INSTRUMENTATION in known

    def test_each_field_carries_a_reason(self):
        """每一项都要有一句"什么时候动它"。

        只给字段名（``top_p``）等于把 OpenAI 的 API 文档摆给用户看。
        """
        for field, label, hint in builder.model_settings_fields():
            assert label and len(hint) > 8, f"{field} 的说明太短"
        for name, label, hint in builder.form_capabilities():
            assert label and hint, f"{name} 缺标签或提示"


class TestBlankIsNotZero:
    """留空与填 0 是两件不同的事。

    混同会把每个没动过参数的 Agent 都变成 ``temperature: 0``——确定性输出，
    而用户不知道自己什么时候选了它。
    """

    def test_blank_fields_are_dropped(self):
        assert (
            builder.model_settings_from_form({"temperature": "", "top_p": "   ", "max_tokens": ""})
            == {}
        )

    def test_explicit_zero_is_kept(self):
        assert builder.model_settings_from_form({"temperature": "0"}) == {"temperature": 0.0}

    def test_integer_fields_stay_integers(self):
        """类型收错的话 ``extra='ignore'`` 会**静默丢掉**那一项。"""
        got = builder.model_settings_from_form(
            {"max_tokens": "2048", "seed": "42", "top_k": "40", "temperature": "0.7"}
        )
        assert isinstance(got["max_tokens"], int) and got["max_tokens"] == 2048
        assert isinstance(got["seed"], int) and got["seed"] == 42
        assert isinstance(got["top_k"], int) and got["top_k"] == 40
        assert got["temperature"] == 0.7

    def test_garbage_says_which_field(self):
        """报错要点名字段——八个输入框里让人自己找是不可接受的。"""
        from xingcha.errors import AgentSpecInvalid

        with pytest.raises(AgentSpecInvalid) as e:
            builder.model_settings_from_form({"temperature": "很热"})
        assert "temperature" in str(e.value)


class TestRoundTrip:
    """spec → 表单 → spec 不能丢东西。

    丢了的话，"改一句提示词再保存"会把之前设过的参数静默清掉。
    """

    def test_settings_survive_a_round_trip(self):
        spec = builder.spec_from_form(
            name="x",
            description=None,
            instructions="i",
            model="m",
            model_settings={"temperature": 0.2, "max_tokens": 4096, "seed": 7},
            capabilities=["Thinking"],
            retries=2,
        )
        view = builder.form_view(spec)
        assert view["settings"]["temperature"] == 0.2
        assert view["settings"]["max_tokens"] == 4096
        assert view["settings"]["seed"] == 7
        assert view["settings"]["top_p"] == "", "没设的项要回填成空串（= 留空）"
        assert view["capabilities"] == {"Thinking"}

    def test_instrumentation_is_detected(self):
        spec = builder.spec_from_form(
            name="x",
            description=None,
            instructions="i",
            model="m",
            capabilities=["Thinking", builder.CAPABILITY_INSTRUMENTATION],
        )
        assert builder.form_view(spec)["instrumented"] is True

    def test_no_capabilities_means_not_instrumented(self):
        spec = builder.spec_from_form(name="x", description=None, instructions="i", model="m")
        assert builder.form_view(spec)["instrumented"] is False

    @pytest.mark.parametrize(
        "caps",
        [
            ["Thinking"],
            [{"name": "Thinking"}],
            [{"Thinking": {"budget": 1024}}],
        ],
        ids=["字符串", "规范形状", "带参数"],
    )
    def test_capability_names_reads_every_shape(self, caps):
        """**capability 在 spec 里有三种形状，反填必须都认。**

        ``validate_spec`` 会把 ``["Thinking"]`` 规范化成
        ``[{"name": "Thinking"}]``——而只认字符串或只取 dict 第一个 key 的实现会
        把它读成一个叫 ``name`` 的能力。后果：**编辑页所有勾都是空的，一保存就把
        用户设过的能力全清掉。** 实测踩过这个 bug。
        """
        assert builder.capability_names(caps) == {"Thinking"}

    def test_round_trip_through_validate_spec(self):
        """经过 ``validate_spec``（真实存库路径）之后仍然认得出来。

        单元测试里对着手写的 spec 断言不够——存库前会过一次规范化，而那正是
        形状变掉的地方。
        """
        spec = builder.validate_spec(
            builder.spec_from_form(
                name="x",
                description=None,
                instructions="i",
                model="openai/gpt-5",
                capabilities=["Thinking", builder.CAPABILITY_INSTRUMENTATION],
            )
        )
        view = builder.form_view(spec)
        assert view["capabilities"] == {"Thinking", builder.CAPABILITY_INSTRUMENTATION}
        assert view["instrumented"] is True


@pytest.fixture
def logged_in(settings: Settings, upstream: FakeUpstream) -> Iterator[TestClient]:
    settings.ensure_data_dir()
    migrate.upgrade_to_head(settings.db_path, settings.backup_dir)
    keyring = Keyring.load_or_create(settings.secret_path)

    async def seed() -> None:
        engine = make_engine(settings.db_path)
        maker = make_sessionmaker(engine)
        async with maker() as s:
            await setting_svc.set_(s, keyring, C.SETTING_KEY_OPENROUTER_API_KEY, "sk-or-v1-fake")
            await setting_svc.set_(s, keyring, C.SETTING_KEY_OPENROUTER_BASE_URL, upstream.base_url)
            await s.commit()
        await engine.dispose()

    asyncio.run(seed())
    upstream.reset()
    # base_url 必须是 https：会话 cookie 带 secure=True
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        r = client.post(
            "/admin/login",
            data={"password": PASSWORD, "confirm": PASSWORD},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text[:200]
        yield client


def csrf_of(client: TestClient) -> str:
    return client.cookies.get("xc_csrf") or ""


class TestFormPage:
    def test_shows_every_section(self, logged_in: TestClient):
        body = logged_in.get("/admin/agents/new").text
        for section in ("基本信息", "提示词", "输出保证", "模型参数", "能力", "可观测"):
            assert section in body, f"新建页缺「{section}」分区"

    def test_model_params_are_rendered(self, logged_in: TestClient):
        body = logged_in.get("/admin/agents/new").text
        for field, _, _ in builder.model_settings_fields():
            assert f'name="ms_{field}"' in body, f"{field} 没出现在表单里"

    def test_capabilities_are_rendered(self, logged_in: TestClient):
        body = logged_in.get("/admin/agents/new").text
        for name, _, _ in builder.form_capabilities():
            assert f'name="cap_{name}"' in body

    def test_observability_switch_is_absent_without_an_endpoint(self, logged_in: TestClient):
        """没配上报地址时不该给一个勾了没用的开关。

        给了的话用户勾上、保存、以为在上报——而什么都没发生。
        """
        body = logged_in.get("/admin/agents/new").text
        assert "还没配上报地址" in body
        assert 'name="instrument"' not in body

    def test_no_inline_script(self, logged_in: TestClient):
        """CSP 是 ``script-src 'self'``，内联脚本会被**静默**挡掉。

        复制按钮那次的教训：页面看起来正常，功能是死的。
        """
        body = logged_in.get("/admin/agents/new").text
        for m in re.finditer(r"<script([^>]*)>(.*?)</script>", body, re.S):
            assert "src=" in m.group(1) or not m.group(2).strip()
        assert not re.search(r"\bon(submit|click|change)\s*=", body)


def _spec_of(settings: Settings, slug: str) -> dict:
    import sqlite3

    with sqlite3.connect(settings.db_path) as c:
        row = c.execute(
            "SELECT v.spec_json FROM agent a JOIN agent_version v ON v.agent_id = a.id "
            "WHERE a.slug = ? ORDER BY v.version DESC LIMIT 1",
            (slug,),
        ).fetchone()
    assert row, f"库里没有 {slug}"
    return json.loads(row[0])


class TestSaveWithNewFields:
    def _create(self, client: TestClient, **extra):
        data = {
            "slug": "params",
            "name": "参数测试",
            "instructions": "做事。",
            "model": "openai/gpt-5",
            "tier": "",
            "retries": "2",
            "csrf_token": csrf_of(client),
            **extra,
        }
        return client.post("/admin/agents/save", data=data, follow_redirects=False)

    def test_model_params_land_in_the_spec(self, logged_in: TestClient, settings: Settings):
        logged_in.get("/admin/agents/new")
        r = self._create(logged_in, ms_temperature="0.2", ms_max_tokens="4096")
        assert r.status_code == 303, r.text[:300]
        assert _spec_of(settings, "params")["model_settings"] == {
            "temperature": 0.2,
            "max_tokens": 4096,
        }

    def test_blank_params_leave_model_settings_absent(
        self, logged_in: TestClient, settings: Settings
    ):
        """全留空时 spec 里**不该有** model_settings 键。

        把"留空就是不设"钉在端到端这一层——单元测试里对了，路由里拼错也白搭。
        """
        logged_in.get("/admin/agents/new")
        assert self._create(logged_in).status_code == 303
        assert "model_settings" not in _spec_of(settings, "params")

    def test_capabilities_land_in_the_spec(self, logged_in: TestClient, settings: Settings):
        logged_in.get("/admin/agents/new")
        r = self._create(logged_in, cap_Thinking="1", cap_WebSearch="1")
        assert r.status_code == 303, r.text[:300]
        caps = builder.capability_names(_spec_of(settings, "params")["capabilities"])
        assert caps == {"Thinking", "WebSearch"}

    def test_bad_number_keeps_what_was_typed(self, logged_in: TestClient):
        """报错回到表单要保留已填内容，**包括模型参数与勾选**。

        只回填前半截的话，用户会以为那些设置没生效、再填一遍。
        """
        logged_in.get("/admin/agents/new")
        r = self._create(logged_in, ms_temperature="很热", cap_Thinking="1")
        assert r.status_code == 200
        assert 'value="很热"' in r.text, "填错的值没有回填"
        one_line = r.text.replace("\n", " ")
        assert re.search(r'name="cap_Thinking"[^>]*checked', one_line), "勾选的能力没有保留"
