"""Agent 端到端：建 Agent → 用 slug 调 → 拿到受校验的结构化输出。

这条链路走的是真实的 HTTP + 真实的 pydantic-ai + 一个真实的本地假上游，
不是 mock。上面单元层的测试证明 guarantee 模块本身对，这里证明它**接进去之后
仍然对**——两者都需要。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any

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
from xingcha.services import auth as auth_svc
from xingcha.services import setting as setting_svc

SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}, "score": {"type": "integer"}},
    "required": ["title", "score"],
}
GOOD = {"title": "标题", "score": 9}
BAD = {"score": "not-an-int"}


@pytest.fixture
def wired(settings: Settings, upstream: FakeUpstream) -> Iterator[tuple[TestClient, str]]:
    settings.ensure_data_dir()
    migrate.upgrade_to_head(settings.db_path, settings.backup_dir)
    keyring = Keyring.load_or_create(settings.secret_path)

    async def seed() -> str:
        engine = make_engine(settings.db_path)
        maker = make_sessionmaker(engine)
        async with maker() as s:
            await setting_svc.set_(s, keyring, C.SETTING_KEY_OPENROUTER_API_KEY, "sk-or-v1-fake")
            await setting_svc.set_(s, keyring, C.SETTING_KEY_OPENROUTER_BASE_URL, upstream.base_url)
            await agent_svc.save(
                s,
                slug="extract",
                name="抽取",
                description="把文本抽成固定字段",
                instructions="从输入里抽取标题与评分。",
                model="openai/gpt-5",
                schema_text=json.dumps(SCHEMA),
                requested_tier=Tier.T2,
                capabilities=None,
                retries=2,
                native_ok=True,
            )
            await agent_svc.save(
                s,
                slug="chat",
                name="纯文本",
                description=None,
                instructions="随便聊。",
                model="openai/gpt-5",
                schema_text=None,
                requested_tier=None,
                capabilities=None,
                retries=2,
                native_ok=True,
            )
            tok = await auth_svc.issue(s, name="t")
            await s.commit()
        await engine.dispose()
        return tok.plaintext

    token = asyncio.run(seed())
    upstream.reset()
    with TestClient(create_app(settings)) as client:
        yield client, token


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# =============================================================================
# Agent 出现在模型列表里
# =============================================================================


class TestAgentsInModelList:
    def test_agent_rows_come_first(self, wired):
        """顺序进了契约：部分客户端取 data[0] 当默认模型。"""
        client, token = wired
        data = client.get("/v1/models", headers=auth(token)).json()["data"]
        assert data[0][C.EXT_KEY]["kind"] == "agent"
        assert data[0]["owned_by"] == C.OWNED_BY_XINGCHA

    def test_agent_row_carries_tier(self, wired):
        client, token = wired
        rows = {r["id"]: r for r in client.get("/v1/models", headers=auth(token)).json()["data"]}
        assert rows["extract"][C.EXT_KEY]["tier"] == Tier.T2.value
        assert rows["extract"][C.EXT_KEY]["structured"] is True
        assert rows["chat"][C.EXT_KEY]["structured"] is False

    def test_retrieve_model_answers_for_agent(self, wired, upstream: FakeUpstream):
        """**这个端点必须由星槎自己回答。**

        归给反代的话，客户端拿 slug 来问会打到上游拿回 404，据此判定「这个模型
        不存在」——而事后收回算破坏性变更，等于永久坏掉。
        """
        client, token = wired
        upstream.reset()
        r = client.get("/v1/models/extract", headers=auth(token))
        assert r.status_code == 200
        assert r.json()["id"] == "extract"

    def test_filter_by_owner(self, wired):
        client, token = wired
        data = client.get("/v1/models?owned_by=xingcha", headers=auth(token)).json()["data"]
        assert {r["id"] for r in data} == {"extract", "chat"}


# =============================================================================
# 结构化 Agent
# =============================================================================


class TestStructuredAgent:
    def test_valid_output_returns_200_with_json_string(self, wired, upstream: FakeUpstream):
        """``message.content`` **永远是字符串**（契约 §3.6）。

        结构化输出是 json.dumps 之后的文本，调用方 json.loads 取回。把 dict 直接
        放进 content 会让所有按 str 处理它的客户端崩掉。
        """
        client, token = wired
        upstream.tool_payloads = [GOOD]
        r = client.post(
            "/v1/chat/completions",
            json={"model": "extract", "messages": [{"role": "user", "content": "文本"}]},
            headers=auth(token),
        )
        assert r.status_code == 200
        content = r.json()["choices"][0]["message"]["content"]
        assert isinstance(content, str)
        assert json.loads(content) == GOOD

    def test_violation_retries_then_422(self, wired, upstream: FakeUpstream):
        """持续违规 → 上游被调用 1+retries 次 → 422 schema_violation。"""
        client, token = wired
        upstream.reset()
        upstream.tool_payloads = [BAD]
        r = client.post(
            "/v1/chat/completions",
            json={"model": "extract", "messages": [{"role": "user", "content": "x"}]},
            headers=auth(token),
        )
        assert r.status_code == 422
        body = r.json()["error"]
        assert body["type"] == C.ErrorType.SCHEMA_VIOLATION.value
        assert body["retries"] == 2
        # 必须说清是哪个字段不对，只说"重试耗尽"没法定位
        assert "title" in body["message"]
        assert upstream.hit_count == 3

    def test_recovers_when_model_fixes_itself(self, wired, upstream: FakeUpstream):
        client, token = wired
        upstream.reset()
        upstream.tool_payloads = [BAD, GOOD]
        r = client.post(
            "/v1/chat/completions",
            json={"model": "extract", "messages": [{"role": "user", "content": "x"}]},
            headers=auth(token),
        )
        assert r.status_code == 200
        assert json.loads(r.json()["choices"][0]["message"]["content"]) == GOOD
        assert upstream.hit_count == 2

    def test_stream_is_rejected_for_structured(self, wired, upstream: FakeUpstream):
        """流一半的 JSON 无法被安全解析。诚实报错优于假装支持。"""
        client, token = wired
        upstream.reset()
        r = client.post(
            "/v1/chat/completions",
            json={
                "model": "extract",
                "messages": [{"role": "user", "content": "x"}],
                "stream": True,
            },
            headers=auth(token),
        )
        assert r.status_code == 400
        assert r.json()["error"]["type"] == C.ErrorType.STREAM_UNSUPPORTED.value
        assert upstream.hit_count == 0

    def test_extension_block_shape(self, wired, upstream: FakeUpstream):
        """所有自有字段的**唯一**落点，且金额是字符串或 null 而不是 number。"""
        client, token = wired
        upstream.tool_payloads = [GOOD]
        body = client.post(
            "/v1/chat/completions",
            json={"model": "extract", "messages": [{"role": "user", "content": "x"}]},
            headers=auth(token),
        ).json()

        ext = body[C.EXT_KEY]
        assert ext["v"] == C.EXT_SHAPE_VERSION
        assert ext["tier"] == Tier.T2.value
        assert ext["cost_usd"] is None or isinstance(ext["cost_usd"], str)
        assert ext["cost_source"] in {s.value for s in C.CostSource}
        # 响应体除 x_xingcha 外不加任何非 OpenAI 键
        assert set(body) - {"id", "object", "created", "model", "choices", "usage"} == {C.EXT_KEY}


# =============================================================================
# 纯文本 Agent
# =============================================================================


class TestTextAgent:
    def test_plain_text_agent_works(self, wired, upstream: FakeUpstream):
        client, token = wired
        r = client.post(
            "/v1/chat/completions",
            json={"model": "chat", "messages": [{"role": "user", "content": "你好"}]},
            headers=auth(token),
        )
        assert r.status_code == 200
        assert r.json()["choices"][0]["message"]["content"] == "hi"

    def test_pseudo_streaming_frame_shape(self, wired):
        """伪流式的帧形状与真流式**完全一致**。

        所以 v0.4 换成真 delta 时对客户端不可见——只是帧数变多，而帧数变多是兼容的。

        为什么现在就发伪流式而不是返回 400：客户端会为那个 400 写死绕过逻辑
        （探测到就改走非流式），等真流式上线时反而打断它们。
        """
        client, token = wired
        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={"model": "chat", "messages": [{"role": "user", "content": "hi"}], "stream": True},
            headers=auth(token),
        ) as r:
            assert r.status_code == 200
            raw = b"".join(r.iter_bytes()).decode()

        frames = [f for f in raw.split("\n\n") if f.strip()]
        assert frames[-1].strip() == "data: [DONE]"

        payloads = [json.loads(f[6:]) for f in frames[:-1]]
        assert payloads[0]["choices"][0]["delta"] == {"role": "assistant"}
        assert payloads[1]["choices"][0]["delta"]["content"] == "hi"
        assert payloads[2]["choices"][0]["finish_reason"] == "stop"
        # 汇总帧带 usage 与扩展块
        assert payloads[-1]["usage"]["total_tokens"] >= 0
        assert payloads[-1][C.EXT_KEY]["v"] == C.EXT_SHAPE_VERSION
        assert all(p["object"] == "chat.completion.chunk" for p in payloads)


# =============================================================================
# 计量
# =============================================================================


class TestAgentMetering:
    def _rows(self, settings: Settings):
        import sqlite3

        with sqlite3.connect(settings.db_path) as c:
            c.row_factory = sqlite3.Row
            return c.execute(
                "SELECT r.kind, r.model, r.status, r.tier, r.agent_id, r.agent_version, "
                "u.input_tokens, u.output_tokens, u.schema_violations, u.schema_retries, "
                "u.cost_usd, u.cost_source "
                "FROM run r JOIN run_usage u ON u.run_id = r.id ORDER BY r.started_at"
            ).fetchall()

    def test_successful_run_is_recorded(self, wired, settings: Settings, upstream: FakeUpstream):
        client, token = wired
        upstream.tool_payloads = [GOOD]
        client.post(
            "/v1/chat/completions",
            json={"model": "extract", "messages": [{"role": "user", "content": "x"}]},
            headers=auth(token),
        )
        client.__exit__(None, None, None)  # 触发关停 flush

        row = self._rows(settings)[-1]
        assert row["kind"] == "agent"
        assert row["model"] == "extract"
        assert row["status"] == "ok"
        assert row["tier"] == Tier.T2.value
        assert row["agent_version"] == 1
        assert row["input_tokens"] > 0

    def test_failed_run_is_also_recorded(self, wired, settings: Settings, upstream: FakeUpstream):
        """**失败也要落用量。**

        一次重试耗尽的调用照样花了钱（1+retries 次模型调用），不记就等于账单少报，
        而且恰好少报的是最贵的那一类。
        """
        client, token = wired
        upstream.reset()
        upstream.tool_payloads = [BAD]
        client.post(
            "/v1/chat/completions",
            json={"model": "extract", "messages": [{"role": "user", "content": "x"}]},
            headers=auth(token),
        )
        client.__exit__(None, None, None)

        row = self._rows(settings)[-1]
        assert row["status"] == "schema_failed"
        assert row["schema_violations"] == 3
        assert row["schema_retries"] == 2


# =============================================================================
# 版本
# =============================================================================


class TestVersioning:
    def test_editing_creates_a_new_version(self, settings: Settings, upstream: FakeUpstream):
        """版本不可变。这是运行时缓存不需要失效逻辑的前提。"""
        settings.ensure_data_dir()
        migrate.upgrade_to_head(settings.db_path, settings.backup_dir)

        async def go() -> tuple[int, int, str]:
            engine = make_engine(settings.db_path)
            maker = make_sessionmaker(engine)
            async with maker() as s:
                first = await agent_svc.save(
                    s,
                    slug="demo",
                    name="v1",
                    description=None,
                    instructions="一",
                    model="openai/gpt-5",
                    schema_text=None,
                    requested_tier=None,
                    capabilities=None,
                    retries=2,
                    native_ok=True,
                )
                second = await agent_svc.save(
                    s,
                    slug="demo",
                    name="v2",
                    description=None,
                    instructions="二",
                    model="openai/gpt-5",
                    schema_text=None,
                    requested_tier=None,
                    capabilities=None,
                    retries=2,
                    native_ok=True,
                )
                await s.commit()
                current = await agent_svc.resolve(s, "demo")
                return first.version, second.version, current.name
            await engine.dispose()

        v1, v2, name = asyncio.run(go())
        assert (v1, v2) == (1, 2)
        assert name == "v2"

    def test_rollback_moves_the_pointer(self, settings: Settings):
        """回滚 = 把 current_version_id 指回去，不改写任何历史版本。"""
        settings.ensure_data_dir()
        migrate.upgrade_to_head(settings.db_path, settings.backup_dir)

        async def go() -> tuple[int, str]:
            engine = make_engine(settings.db_path)
            maker = make_sessionmaker(engine)
            async with maker() as s:
                r1 = await agent_svc.save(
                    s,
                    slug="demo",
                    name="v1",
                    description=None,
                    instructions="一",
                    model="openai/gpt-5",
                    schema_text=None,
                    requested_tier=None,
                    capabilities=None,
                    retries=2,
                    native_ok=True,
                )
                await agent_svc.save(
                    s,
                    slug="demo",
                    name="v2",
                    description=None,
                    instructions="二",
                    model="openai/gpt-5",
                    schema_text=None,
                    requested_tier=None,
                    capabilities=None,
                    retries=2,
                    native_ok=True,
                )
                await agent_svc.rollback(s, r1.agent_id, 1)
                await s.commit()
                cur = await agent_svc.resolve(s, "demo")
                versions = await agent_svc.versions(s, r1.agent_id)
                return len(versions), json.loads(cur.spec_json)["instructions"]

        count, instructions = asyncio.run(go())
        assert count == 2, "回滚不该删掉任何版本"
        assert instructions == "一"


def test_unknown_slug_never_reaches_upstream(wired, upstream: FakeUpstream):
    """**绝不回落上游。**

    查不到就当上游模型转发出去的话，一个拼错的 slug 会静默变成一次真实的付费调用，
    而调用方以为自己在调 Agent。
    """
    client, token = wired
    upstream.reset()
    r = client.post(
        "/v1/chat/completions",
        json={"model": "nonexistent", "messages": [{"role": "user", "content": "x"}]},
        headers=auth(token),
    )
    assert r.status_code == 404
    assert upstream.hit_count == 0


# =============================================================================
# T1 与 T1+
# =============================================================================


OPTIONAL_SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}, "score": {"type": "integer"}},
    "required": ["title"],  # score 是可选的
}


async def _make(session, slug: str, tier: Tier, schema: dict | None) -> None:
    await agent_svc.save(
        session,
        slug=slug,
        name=slug,
        description=None,
        instructions="做事。",
        model="openai/gpt-5",
        schema_text=json.dumps(schema) if schema else None,
        requested_tier=tier,
        capabilities=None,
        retries=2,
        native_ok=True,
    )


@pytest.fixture
def tiered(settings: Settings, upstream: FakeUpstream) -> Iterator[tuple[TestClient, str]]:
    settings.ensure_data_dir()
    migrate.upgrade_to_head(settings.db_path, settings.backup_dir)
    keyring = Keyring.load_or_create(settings.secret_path)

    async def seed() -> str:
        engine = make_engine(settings.db_path)
        maker = make_sessionmaker(engine)
        async with maker() as s:
            await setting_svc.set_(s, keyring, C.SETTING_KEY_OPENROUTER_API_KEY, "sk-or-v1-fake")
            await setting_svc.set_(s, keyring, C.SETTING_KEY_OPENROUTER_BASE_URL, upstream.base_url)
            await _make(s, "native", Tier.T1, OPTIONAL_SCHEMA)
            await _make(s, "twostage", Tier.T1P, OPTIONAL_SCHEMA)
            await _make(s, "prompted", Tier.T3, OPTIONAL_SCHEMA)
            tok = await auth_svc.issue(s, name="t")
            await s.commit()
        await engine.dispose()
        return tok.plaintext

    token = asyncio.run(seed())
    upstream.reset()
    with TestClient(create_app(settings)) as client:
        yield client, token


def _call(client: TestClient, token: str, model: str) -> Any:
    return client.post(
        "/v1/chat/completions",
        json={"model": model, "messages": [{"role": "user", "content": "输入"}]},
        headers=auth(token),
    )


class TestT1Native:
    def test_uses_json_schema_response_format(self, tiered, upstream: FakeUpstream):
        client, token = tiered
        upstream.reset()
        upstream.tool_payloads = [{"title": "t", "score": 1}]
        r = _call(client, token, "native")
        assert r.status_code == 200
        assert upstream.native_requests, "T1 必须走 response_format 通道"
        assert upstream.native_requests[0]["json_schema"]["strict"] is True

    def test_optional_fields_are_silently_promoted(self, tiered, upstream: FakeUpstream):
        """**这是 T1 唯一真正的代价，而且用户在表单上看不出来。**

        strict=True 会把可选字段塞进 required：用户标为可选的 score 变成模型**必须**
        输出的字段。这条测试把这件事钉住——如果哪天上游改了行为，我们该知道。
        """
        client, token = tiered
        upstream.reset()
        upstream.tool_payloads = [{"title": "t", "score": 1}]
        _call(client, token, "native")

        sent = upstream.native_requests[0]["json_schema"]["schema"]["required"]
        assert set(sent) == {"title", "score"}
        assert OPTIONAL_SCHEMA["required"] == ["title"], "用户定义的其实只有 title"

    def test_still_validates_locally(self, tiered, upstream: FakeUpstream):
        """T1 下仍然挂本地校验。

        上游没兑现 strict 时（真实发生过），本地校验是最后一道——绝不把脏数据
        交给调用方。
        """
        client, token = tiered
        upstream.reset()
        upstream.tool_payloads = [{"score": "not-an-int"}]
        r = _call(client, token, "native")
        assert r.status_code == 422


class TestT1PlusTwoStage:
    def test_calls_the_model_twice(self, tiered, upstream: FakeUpstream):
        """先自由推理再格式化。第一步不带任何格式约束，规避对齐税。"""
        client, token = tiered
        upstream.reset()
        upstream.tool_payloads = [{"title": "t", "score": 2}]
        r = _call(client, token, "twostage")
        assert r.status_code == 200
        assert upstream.hit_count == 2

    def test_first_stage_has_no_format_constraint(self, tiered, upstream: FakeUpstream):
        """两阶段的全部意义就在这一条：推理那一步不受格式约束干扰。"""
        client, token = tiered
        upstream.reset()
        upstream.tool_payloads = [{"title": "t", "score": 2}]
        _call(client, token, "twostage")

        first = json.loads(upstream.requests[0].body)
        second = json.loads(upstream.requests[1].body)
        assert "response_format" not in first and "tools" not in first
        assert second.get("response_format", {}).get("type") == "json_schema"

    def test_second_stage_is_told_not_to_change_facts(self, tiered, upstream: FakeUpstream):
        """第二步顺手"改进"内容的话，就等于又引入一次未受控的生成，比 T1 更糟。"""
        client, token = tiered
        upstream.reset()
        upstream.tool_payloads = [{"title": "t", "score": 2}]
        _call(client, token, "twostage")

        second = json.loads(upstream.requests[1].body)
        text = json.dumps(second["messages"], ensure_ascii=False)
        assert "不要新增事实" in text

    def test_usage_covers_both_stages(self, tiered, upstream: FakeUpstream):
        """两阶段的用量必须**累加**。

        只记第二步的话，第一步（自由推理，往往是更贵的一步）完全不进账单——
        而那正是这一档比 T1 贵一倍的原因所在，账单上却看不出来。

        断言的是"两阶段比单阶段贵"这个性质，而不是写死的数字：写死数字的测试在
        假上游改一次返回值时就会红，而它本该关心的不是那个。
        """
        client, token = tiered

        upstream.reset()
        upstream.tool_payloads = [{"title": "t", "score": 2}]
        single = _call(client, token, "native").json()["usage"]

        upstream.reset()
        upstream.tool_payloads = [{"title": "t", "score": 2}]
        double = _call(client, token, "twostage").json()["usage"]

        assert upstream.hit_count == 2
        assert double["prompt_tokens"] > single["prompt_tokens"]
        assert double["completion_tokens"] > single["completion_tokens"]


class TestT3Prompted:
    def test_no_schema_constraint_and_no_validation(self, tiered, upstream: FakeUpstream):
        """T3 明确不校验——这是设计，不是缺陷。违规数据原样返回。"""
        client, token = tiered
        upstream.reset()
        upstream.tool_payloads = [{"score": "not-an-int"}]
        r = _call(client, token, "prompted")
        assert r.status_code == 200
        assert json.loads(r.json()["choices"][0]["message"]["content"]) == {"score": "not-an-int"}
        assert upstream.hit_count == 1
