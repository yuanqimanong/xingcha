"""上游费用对账。

------------------------------------------------------------------------------
为什么这件事需要单独一层
------------------------------------------------------------------------------

pydantic-ai 会自动填 ``RunUsage.cost``，但那是 genai-prices 按公开价目算的**估价**。
中转（OpenRouter 及各类兼容网关）在响应体里报的 ``usage.cost`` 才是要真金白银付的
数字，而这两个数可以差几百倍——中转加价、缓存折扣、按 provider 路由的实际单价，
估价一概不知道。

更糟的是上游那个真值**在 pydantic-ai 里一点痕迹都不留**：它是 float，被
``_map_usage`` 的 ``isinstance(v, int)`` 过滤掉了。所以只能在 HTTP 层抓。

这个文件要证明的三件事：

1. 上游报了费用时，落库的是**上游那个数**，``cost_source`` 说 ``upstream``；
2. 上游没报时，回落目录价而**不是记成 0**——记成 0 会让配额永远花不完；
3. 抓费用这件事**不会影响主调用**：钩子炸了、流式响应、上游 4xx，都不能连带出错。
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal

import pytest

from conftest import FakeUpstream
from test_agent_e2e import GOOD, auth, wired  # noqa: F401  (wired 是 fixture)
from xingcha import contract as C
from xingcha.config import Settings
from xingcha.core.costsink import CostSink, _cost_from_body, make_hook

# 一个绝不可能是目录价算出来的数字：假上游的 token 数很小，目录价只有 1e-5 量级。
UPSTREAM_COST = "0.0271"


def usage_rows(settings: Settings) -> list[sqlite3.Row]:
    with sqlite3.connect(settings.db_path) as c:
        c.row_factory = sqlite3.Row
        return c.execute(
            "SELECT r.kind, r.model, u.cost_usd, u.cost_source, u.extra_json "
            "FROM run r JOIN run_usage u ON u.run_id = r.id ORDER BY r.started_at"
        ).fetchall()


# =============================================================================
# CostSink 本身
# =============================================================================


class TestCostSink:
    def test_take_removes(self):
        sink = CostSink()
        sink.put("a", Decimal("1.5"))
        assert sink.take(["a"]) == Decimal("1.5")
        assert sink.take(["a"]) is None, "取走即删，否则同一笔费用会被记两遍"

    def test_miss_is_none_not_zero(self):
        """没命中必须是 None。

        返回 Decimal(0) 的话调用方分不清"上游说这次不要钱"和"上游没说"，
        于是会把一次真花了钱的调用记成免费。
        """
        assert CostSink().take(["nope"]) is None

    def test_multiple_ids_sum(self):
        """一次运行可能有多次上游调用（重试、工具往返、两阶段），费用要加起来。"""
        sink = CostSink()
        sink.put("a", Decimal("0.01"))
        sink.put("b", Decimal("0.02"))
        assert sink.take(["a", "b"]) == Decimal("0.03")

    def test_same_id_accumulates(self):
        sink = CostSink()
        sink.put("a", Decimal("0.01"))
        sink.put("a", Decimal("0.02"))
        assert sink.take(["a"]) == Decimal("0.03")

    def test_bounded(self):
        """取走的那一步可能永远不发生（超时、进程被 kill），所以必须有界。"""
        sink = CostSink(max_entries=4)
        for i in range(50):
            sink.put(f"r{i}", Decimal("0.01"))
        assert len(sink) == 4
        assert sink.take(["r0"]) is None, "最老的应当被挤掉"
        assert sink.take(["r49"]) == Decimal("0.01")

    @pytest.mark.parametrize(
        "body",
        [
            b"not json",
            b"[]",
            b'{"usage": null}',
            b'{"usage": {}}',
            b'{"usage": {"cost": "abc"}}',
            b'{"usage": {"cost": -1}}',
            b'{"usage": {"cost": {"total": 1}}}',
        ],
    )
    def test_garbage_bodies_yield_nothing(self, body: bytes):
        """上游是别人的服务，什么都可能返回。解析失败只能是"没有费用"。"""
        assert _cost_from_body(body) is None

    def test_float_precision_goes_through_str(self):
        """float 直接进 Decimal 会带二进制误差，必须先 str()。"""
        assert _cost_from_body(b'{"usage":{"cost":0.1}}') == Decimal("0.1")

    @pytest.mark.anyio
    async def test_hook_never_raises(self):
        """钩子是记账的，不是主链路。它炸掉不能把一次成功的调用变成 500。"""

        class Exploding:
            @property
            def headers(self):
                raise RuntimeError("boom")

        await make_hook(CostSink())(Exploding())  # 不抛就算过


# =============================================================================
# Agent 路径
# =============================================================================


class TestAgentPath:
    def test_upstream_cost_wins(self, wired, settings: Settings, upstream: FakeUpstream):  # noqa: F811
        client, token = wired
        upstream.tool_payloads = [GOOD]
        upstream.report_cost = UPSTREAM_COST

        body = client.post(
            "/v1/chat/completions",
            json={"model": "extract", "messages": [{"role": "user", "content": "x"}]},
            headers=auth(token),
        ).json()
        assert body[C.EXT_KEY]["cost_source"] == C.CostSource.UPSTREAM.value
        assert Decimal(body[C.EXT_KEY]["cost_usd"]) == Decimal(UPSTREAM_COST)

        client.__exit__(None, None, None)  # 触发关停 flush
        row = usage_rows(settings)[-1]
        assert row["cost_source"] == C.CostSource.UPSTREAM.value
        assert Decimal(row["cost_usd"]) == Decimal(UPSTREAM_COST)

    def test_estimate_and_delta_are_kept(self, wired, settings: Settings, upstream: FakeUpstream):  # noqa: F811
        """预估也要留着。

        只留实际值的话，"目录价准不准"这个问题永远只能靠人工翻账单回答。
        """
        client, token = wired
        upstream.tool_payloads = [GOOD]
        upstream.report_cost = UPSTREAM_COST
        client.post(
            "/v1/chat/completions",
            json={"model": "extract", "messages": [{"role": "user", "content": "x"}]},
            headers=auth(token),
        )
        client.__exit__(None, None, None)

        extra = json.loads(usage_rows(settings)[-1]["extra_json"])
        estimate = Decimal(extra["cost_estimate"])
        assert estimate > 0
        assert estimate != Decimal(UPSTREAM_COST), "假上游的目录价不该恰好等于报价"
        assert Decimal(extra["cost_delta"]) == Decimal(UPSTREAM_COST) - estimate

    def test_falls_back_to_catalog_when_silent(
        self,
        wired,  # noqa: F811
        settings: Settings,
        upstream: FakeUpstream,
    ):
        """上游不报费用时回落目录价，而不是记成 0。"""
        client, token = wired
        upstream.tool_payloads = [GOOD]
        upstream.report_cost = None

        body = client.post(
            "/v1/chat/completions",
            json={"model": "extract", "messages": [{"role": "user", "content": "x"}]},
            headers=auth(token),
        ).json()
        assert body[C.EXT_KEY]["cost_source"] == C.CostSource.CATALOG.value
        assert Decimal(body[C.EXT_KEY]["cost_usd"]) > 0

        client.__exit__(None, None, None)
        row = usage_rows(settings)[-1]
        assert row["cost_source"] == C.CostSource.CATALOG.value
        assert "cost_estimate" not in (row["extra_json"] or "")

    def test_retries_accumulate_cost(self, wired, settings: Settings, upstream: FakeUpstream):  # noqa: F811
        """schema 违规重试也要付钱。

        只算最后一次的话，越是不听话的模型看起来越便宜——正好搞反了。
        """
        from test_agent_e2e import BAD

        client, token = wired
        upstream.tool_payloads = [BAD, GOOD]
        upstream.report_cost = UPSTREAM_COST

        body = client.post(
            "/v1/chat/completions",
            json={"model": "extract", "messages": [{"role": "user", "content": "x"}]},
            headers=auth(token),
        ).json()
        assert upstream.chat_count == 2
        assert Decimal(body[C.EXT_KEY]["cost_usd"]) == Decimal(UPSTREAM_COST) * 2

    def test_streaming_does_not_break(self, wired, upstream: FakeUpstream):  # noqa: F811
        """带费用的流式仍然是流式。

        钩子若在流式响应上读 body，就把流消费掉了，客户端只会收到空流。
        """
        client, token = wired
        upstream.report_cost = UPSTREAM_COST
        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={"model": "chat", "messages": [{"role": "user", "content": "hi"}], "stream": True},
            headers=auth(token),
        ) as r:
            assert r.status_code == 200
            raw = b"".join(r.iter_bytes()).decode()
        assert raw.strip().endswith("data: [DONE]")
        assert "012" in raw

    def test_agent_streaming_stays_on_catalog_price(
        self,
        wired,  # noqa: F811
        settings: Settings,
        upstream: FakeUpstream,
    ):
        """**已知限制：Agent 真流式拿不到上游实价。**

        钩子必须跳过 event-stream（读 body 就把流吃掉了），而流式的实价在最后一帧
        里，那一帧是被 pydantic-ai 消费掉的——它把 usage 解析出来，但 ``cost`` 是
        float，又被 ``isinstance(v, int)`` 过滤了。

        所以这里落的是目录估价。写成一条断言而不是注释：将来谁真把这条链路补上，
        这个测试会红，正好提醒他改文档；在那之前，谁也不会误以为流式的费用是实价。

        对账影响有限——直通路径的流式仍然能拿到实价（尾帧嗅探），而 Agent 的主要
        用法（结构化抽取）根本不支持流式。
        """
        client, token = wired
        upstream.report_cost = UPSTREAM_COST
        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={"model": "chat", "messages": [{"role": "user", "content": "x"}], "stream": True},
            headers=auth(token),
        ) as r:
            b"".join(r.iter_bytes())
        client.__exit__(None, None, None)

        row = usage_rows(settings)[-1]
        assert row["cost_source"] == C.CostSource.CATALOG.value
        assert Decimal(row["cost_usd"]) != Decimal(UPSTREAM_COST)


# =============================================================================
# 直通路径
# =============================================================================


class TestPassthroughPath:
    """直通路径不经过 pydantic-ai，费用直接从转发的响应体里读。

    两条路径的 ``cost_source`` 口径必须一致——否则账单里同一个字段在不同行里
    含义不同，对账时没法一起 SUM。
    """

    def test_upstream_cost_wins(self, wired, settings: Settings, upstream: FakeUpstream):  # noqa: F811
        client, token = wired
        upstream.report_cost = UPSTREAM_COST
        r = client.post(
            "/v1/chat/completions",
            json={"model": "openai/gpt-5", "messages": [{"role": "user", "content": "x"}]},
            headers=auth(token),
        )
        assert r.status_code == 200
        assert C.EXT_KEY not in r.json(), "直通必须是字节级原样转发，不加自有字段"

        client.__exit__(None, None, None)
        row = usage_rows(settings)[-1]
        assert row["kind"] == "passthrough"
        assert row["cost_source"] == C.CostSource.UPSTREAM.value
        assert Decimal(row["cost_usd"]) == Decimal(UPSTREAM_COST)
        assert Decimal(json.loads(row["extra_json"])["cost_estimate"]) > 0

    def test_falls_back_to_catalog_when_silent(
        self,
        wired,  # noqa: F811
        settings: Settings,
        upstream: FakeUpstream,
    ):
        client, token = wired
        upstream.report_cost = None
        client.post(
            "/v1/chat/completions",
            json={"model": "openai/gpt-5", "messages": [{"role": "user", "content": "x"}]},
            headers=auth(token),
        )
        client.__exit__(None, None, None)
        row = usage_rows(settings)[-1]
        assert row["cost_source"] == C.CostSource.CATALOG.value
        assert Decimal(row["cost_usd"]) > 0

    def test_streaming_tail_carries_cost(
        self,
        wired,  # noqa: F811
        settings: Settings,
        upstream: FakeUpstream,
    ):
        """流式的费用在最后一帧里。

        转发时不能为了读它而把流物化——所以只嗅探尾部，且嗅探不改变吐给客户端的字节。
        """
        client, token = wired
        upstream.report_cost = UPSTREAM_COST
        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={"model": "openai/gpt-5", "stream": True},
            headers=auth(token),
        ) as r:
            assert r.status_code == 200
            raw = b"".join(r.iter_bytes()).decode()
        assert raw.strip().endswith("data: [DONE]")

        client.__exit__(None, None, None)
        row = usage_rows(settings)[-1]
        assert row["cost_source"] == C.CostSource.UPSTREAM.value
        assert Decimal(row["cost_usd"]) == Decimal(UPSTREAM_COST)


# =============================================================================
# 与配额的接缝
# =============================================================================


def test_quota_settles_against_the_real_cost(settings: Settings, upstream: FakeUpstream):
    """金额配额扣的是**上游报的钱**，不是目录估价。

    这是整个对账功能的落点：一条 $0.05/天 的规则，若按估价扣（假上游这里差三个
    数量级），要花掉几百次调用才会触发——名义上"有钱刹车"，实际上刹不住。
    """
    import asyncio

    from xingcha.app import create_app
    from xingcha.crypto import Keyring
    from xingcha.db import migrate
    from xingcha.db.engine import make_engine, make_sessionmaker
    from xingcha.services import agent as agent_svc
    from xingcha.services import auth as auth_svc
    from xingcha.services import quota as quota_svc
    from xingcha.services import setting as setting_svc

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
                name="x",
                description=None,
                instructions="i",
                model="openai/gpt-5",
                schema_text=json.dumps(
                    {"type": "object", "properties": {"title": {"type": "string"}}}
                ),
                requested_tier=C.Tier.T2,
                capabilities=None,
                retries=2,
                native_ok=True,
            )
            await quota_svc.upsert(
                s,
                subject_type="user",
                subject_id=1,
                window="day",
                limit_usd=Decimal("0.05"),
                limit_requests=None,
            )
            tok = await auth_svc.issue(s, name="t")
            await s.commit()
        await engine.dispose()
        return tok.plaintext

    token = asyncio.run(seed())
    upstream.reset()
    upstream.tool_payloads = [{"title": "ok"}]
    upstream.report_cost = UPSTREAM_COST  # $0.0271/次

    app = create_app(settings)
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        codes = [
            client.post(
                "/v1/chat/completions",
                json={"model": "extract", "messages": [{"role": "user", "content": "x"}]},
                headers=auth(token),
            ).status_code
            for _ in range(3)
        ]
        spent = Decimal(app.state.xc.quota.snapshot()[0]["spent_usd"])

    # 0.0271 × 2 = 0.0542 > 0.05，所以第三次必须被拦。
    assert codes == [200, 200, 429]
    assert spent == Decimal(UPSTREAM_COST) * 2
