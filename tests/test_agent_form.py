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


# =============================================================================
# 用户提示词模板与少样本
# =============================================================================


class TestPrompting:
    """系统提示词之外的两件事。

    ``AgentSpec`` 只有 ``instructions``，所以这两项存在 ``metadata`` 的星槎命名
    空间下——那是官方 schema 里唯一允许放自定义内容的地方（顶层是
    ``additionalProperties: false``）。
    """

    def test_template_without_the_placeholder_is_refused(self):
        """**必须拦下。**

        模板非空却没有占位符，等于调用方发来的内容被整个丢掉：每次调用都拿同一段
        固定文本去问模型。表现是"Agent 好像不看我的输入"，而表单上一切正常。
        """
        from xingcha.errors import AgentSpecInvalid

        with pytest.raises(AgentSpecInvalid) as e:
            builder.validate_prompting("请抽取合同信息。", [])
        assert builder.PROMPT_PLACEHOLDER in str(e.value)

    def test_half_filled_example_is_refused(self):
        from xingcha.errors import AgentSpecInvalid

        with pytest.raises(AgentSpecInvalid):
            builder.validate_prompting("", [builder.Example("问", "")])

    def test_empty_prompting_writes_no_metadata_key(self):
        """空的不写键——每个 spec 里多一坨空结构，导出的 agent.yaml 也跟着脏。"""
        spec = builder.spec_from_form(
            name="x",
            description=None,
            instructions="i",
            model="m",
            prompting=builder.Prompting(),
        )
        assert "metadata" not in spec

    def test_survives_validate_spec_round_trip(self):
        """必须过**真实存库路径**。

        ``AgentSpec`` 的官方 schema 是 ``additionalProperties: false``，顶层加字段
        会被直接打回；而 ``extra='ignore'`` 又让形状错的东西被静默吞掉。两条都得
        在这里证伪。
        """
        prompting = builder.Prompting(
            user_template="请抽取：\n\n{{input}}",
            examples=(builder.Example("甲方是谁", '{"甲方": "某某"}'),),
        )
        spec = builder.validate_spec(
            builder.spec_from_form(
                name="x",
                description=None,
                instructions="i",
                model="openai/gpt-5",
                prompting=prompting,
            )
        )
        back = builder.prompting_from_spec(spec)
        assert back.user_template == prompting.user_template
        assert back.examples == prompting.examples

    def test_reading_a_malformed_spec_degrades_to_empty(self):
        """读取要宽容：库里可能存着更早版本写的 spec。

        一个老 Agent 不该因为 metadata 里少个键就整个跑不起来。
        """
        assert builder.prompting_from_spec({}).is_empty
        assert builder.prompting_from_spec({"metadata": {"xingcha": "不是字典"}}).is_empty
        assert builder.prompting_from_spec(
            {"metadata": {"xingcha": {"examples": [{"user": "只有问"}]}}}
        ).is_empty

    def test_form_saves_and_reloads_them(self, logged_in: TestClient, settings: Settings):
        logged_in.get("/admin/agents/new")
        r = logged_in.post(
            "/admin/agents/save",
            data={
                "slug": "framed",
                "name": "带模板",
                "instructions": "做事。",
                "model": "openai/gpt-5",
                "tier": "",
                "retries": "2",
                "user_template": "请抽取：\n\n{{input}}",
                "ex_user": ["甲方是谁", "乙方是谁"],
                "ex_assistant": ["某某", "另一个"],
                "csrf_token": csrf_of(logged_in),
            },
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text[:400]
        got = builder.prompting_from_spec(_spec_of(settings, "framed"))
        assert got.user_template == "请抽取：\n\n{{input}}"
        assert [e.user for e in got.examples] == ["甲方是谁", "乙方是谁"]

        # 编辑页要回填，否则"只想改一句提示词"会把模板和示例静默清掉
        body = logged_in.get("/admin/agents/framed").text
        assert "请抽取：" in body and "甲方是谁" in body

    def test_a_bad_template_keeps_what_was_typed(self, logged_in: TestClient):
        logged_in.get("/admin/agents/new")
        r = logged_in.post(
            "/admin/agents/save",
            data={
                "slug": "framed",
                "name": "带模板",
                "instructions": "做事。",
                "model": "openai/gpt-5",
                "tier": "",
                "retries": "2",
                "user_template": "没有占位符",
                "ex_user": "问",
                "ex_assistant": "答",
                "csrf_token": csrf_of(logged_in),
            },
            follow_redirects=False,
        )
        assert r.status_code == 200
        assert "没有占位符" in r.text, "填错的模板没有回填"
        assert "问" in r.text and "答" in r.text, "示例没有保留"


# =============================================================================
# 分组
# =============================================================================


class TestGroups:
    """分组只是 Agent 上的一个字符串，**不影响任何解析**。

    一旦它能影响 slug 或 /v1/models，改个分组就静默改变了调用方看到的 model id ——
    而那是这个项目最不能发生的一类变化（"部署之后使用方式永不改变"）。
    """

    def _create(self, client: TestClient, slug: str, **extra):
        client.get("/admin/agents/new")
        return client.post(
            "/admin/agents/save",
            data={
                "slug": slug,
                "name": slug,
                "instructions": "做事。",
                "model": "openai/gpt-5",
                "tier": "",
                "retries": "2",
                "csrf_token": csrf_of(client),
                **extra,
            },
            follow_redirects=False,
        )

    def _group_of(self, settings: Settings, slug: str):
        import sqlite3

        with sqlite3.connect(settings.db_path) as c:
            return c.execute("SELECT group_name FROM agent WHERE slug = ?", (slug,)).fetchone()[0]

    def test_no_group_stays_null_not_a_literal_default(
        self, logged_in: TestClient, settings: Settings
    ):
        """库里存 NULL，不是"默认分组"四个字。

        存字面值的话，"没分过组"和"被明确放进一个叫默认分组的组"就再也分不开了。
        """
        assert self._create(logged_in, "ungrouped").status_code == 303
        assert self._group_of(settings, "ungrouped") is None
        assert "默认分组" in logged_in.get("/admin/agents").text

    def test_a_group_lands_and_shows_up(self, logged_in: TestClient, settings: Settings):
        assert self._create(logged_in, "billed", group="财务").status_code == 303
        assert self._group_of(settings, "billed") == "财务"
        assert "财务" in logged_in.get("/admin/agents").text

    def test_the_group_never_reaches_the_model_list(
        self, logged_in: TestClient, settings: Settings
    ):
        """分组不进 /v1/models。进了的话改分组 = 改 model id。"""
        self._create(logged_in, "billed", group="财务")
        spec = _spec_of(settings, "billed")
        assert "财务" not in json.dumps(spec, ensure_ascii=False), "分组漏进 spec 了"

    def test_clearing_the_group_moves_it_back(self, logged_in: TestClient, settings: Settings):
        """保存时按表单来，包括清空。

        只在非空时才写的话，"把它挪回默认组"这个动作根本做不了。
        """
        self._create(logged_in, "billed", group="财务")
        self._create(logged_in, "billed", group="")
        assert self._group_of(settings, "billed") is None

    def test_renaming_moves_the_whole_group(self, logged_in: TestClient, settings: Settings):
        self._create(logged_in, "a1", group="旧名")
        self._create(logged_in, "a2", group="旧名")
        logged_in.get("/admin/agents")
        r = logged_in.post(
            "/admin/agents/group/rename",
            data={"csrf_token": csrf_of(logged_in), "old": "旧名", "new": "新名"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert self._group_of(settings, "a1") == "新名"
        assert self._group_of(settings, "a2") == "新名"

    def test_toggle_disables_without_deleting(self, logged_in: TestClient, settings: Settings):
        """停用**不删**：调用方代码里写着这个 slug。

        删掉的话它们收到 model_not_found，而且 slug 会被释放出去、可能被别的
        Agent 顶替——那就不是可逆的了。
        """
        import sqlite3

        self._create(logged_in, "pausable")
        logged_in.get("/admin/agents")
        r = logged_in.post(
            "/admin/agents/pausable/toggle",
            data={"csrf_token": csrf_of(logged_in)},
            follow_redirects=False,
        )
        assert r.status_code == 303
        with sqlite3.connect(settings.db_path) as c:
            active = c.execute(
                "SELECT is_active FROM agent WHERE slug = ?", ("pausable",)
            ).fetchone()[0]
        assert not active

        # slug 仍然被占着——这才是"可逆"的含义
        import asyncio

        from xingcha.services import agent as agent_svc

        state = logged_in.app.state.xc  # type: ignore[attr-defined]

        async def taken():
            async with state.sessionmaker() as s:
                return await agent_svc.slug_available(s, "pausable")

        assert asyncio.run(taken()) is False, "停用把 slug 释放了，别的 Agent 能顶掉它"

        # 而且能开回来
        logged_in.get("/admin/agents")
        logged_in.post(
            "/admin/agents/pausable/toggle",
            data={"csrf_token": csrf_of(logged_in)},
            follow_redirects=False,
        )
        with sqlite3.connect(settings.db_path) as c:
            assert c.execute(
                "SELECT is_active FROM agent WHERE slug = ?", ("pausable",)
            ).fetchone()[0]


# =============================================================================
# 试运行
# =============================================================================


class TestTryRun:
    """按**表单里此刻的内容**跑一次，不落库。

    测试的对象是没保存的东西——不然"改一句提示词看看效果"就得先保存，而每试一次
    就多一个不可删的版本。
    """

    def _run(self, client: TestClient, **extra):
        client.get("/admin/agents/new")
        return client.post(
            "/admin/agents/test",
            data={
                "csrf_token": csrf_of(client),
                "name": "试",
                "model": "openai/gpt-5",
                "instructions": "做事。",
                "tier": "",
                "retries": "2",
                "test_input": "你好",
                **extra,
            },
        )

    def test_it_renders_the_real_chain(self, logged_in: TestClient):
        """渲染的是 ``all_messages()``——上游实际收发的东西。

        照表单重建的"应该发什么"没有价值：两者分叉的那一刻正好是最需要看这个
        面板的时候。
        """
        r = self._run(logged_in)
        assert r.status_code == 200, r.text[:300]
        assert "chain-row" in r.text, r.text[:400]
        assert "系统指令" in r.text

    def test_it_does_not_save_anything(self, logged_in: TestClient, settings: Settings):
        import sqlite3

        self._run(logged_in, slug="never-saved")
        with sqlite3.connect(settings.db_path) as c:
            assert not c.execute("SELECT 1 FROM agent").fetchall(), "试运行把 Agent 存进去了"

    def test_an_empty_input_says_what_to_do(self, logged_in: TestClient):
        r = self._run(logged_in, test_input="")
        assert "先填一段测试输入" in r.text

    def test_a_bad_template_is_reported_not_crashed(self, logged_in: TestClient):
        r = self._run(logged_in, user_template="没有占位符")
        assert r.status_code == 200
        assert builder.PROMPT_PLACEHOLDER in r.text

    def test_the_template_and_examples_reach_the_model(
        self, logged_in: TestClient, upstream: FakeUpstream
    ):
        """判据是**上游收到了什么**，不是星槎内部结构长什么样。"""
        upstream.reset()
        r = self._run(
            logged_in,
            user_template="请判断情绪：{{input}}",
            ex_user="糟透了",
            ex_assistant="负面",
        )
        assert r.status_code == 200, r.text[:300]
        sent = json.loads(upstream.last().body)["messages"]
        convo = [(m["role"], m["content"]) for m in sent if m["role"] != "system"]
        assert convo == [
            ("user", "请判断情绪：糟透了"),
            ("assistant", "负面"),
            ("user", "请判断情绪：你好"),
        ], sent
