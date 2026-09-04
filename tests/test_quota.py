"""配额执行。

**这是星槎里唯一真正的钱刹车**（v0.3 之前只有 OpenRouter 侧的信用上限），
所以三件事必须都成立：拦得住、不漏算、重启不归零。

其中"不漏算"最容易被写错：用量是异步批量落库的，配额若从数据库读求和，
刚发生的调用还没落盘就会被漏掉——在一次突发里能漏掉整批。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

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
from xingcha.errors import QuotaExceeded
from xingcha.services import agent as agent_svc
from xingcha.services import auth as auth_svc
from xingcha.services import quota as quota_svc
from xingcha.services import setting as setting_svc
from xingcha.services.quota import QuotaService, window_key, window_start

SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}},
    "required": ["title"],
}
GOOD = {"title": "ok"}


# =============================================================================
# 窗口口径
# =============================================================================


class TestWindows:
    def test_keys_are_utc(self):
        """用本地时区会让"今天"的边界随部署机时区变化，跨时区对账时对不上。"""
        now = datetime(2026, 3, 15, 23, 30, tzinfo=UTC)
        assert window_key("day", now=now) == "2026-03-15"
        assert window_key("month", now=now) == "2026-03"
        assert window_key("total", now=now) == "all"

    def test_day_rolls_over_at_utc_midnight(self):
        before = datetime(2026, 3, 15, 23, 59, tzinfo=UTC)
        after = before + timedelta(minutes=2)
        assert window_key("day", now=before) != window_key("day", now=after)

    def test_total_never_rolls(self):
        a = datetime(2020, 1, 1, tzinfo=UTC)
        b = datetime(2030, 1, 1, tzinfo=UTC)
        assert window_key("total", now=a) == window_key("total", now=b)

    def test_total_has_no_start(self):
        assert window_start("total") is None
        assert window_start("day") is not None

    def test_spent_resets_when_period_changes(self):
        """翻滚不靠定时任务：窗口标识和已用量存在一起，标识变了就归零。

        定时任务需要调度器，而 C1 说零中间件。
        """
        spent = quota_svc.Spent(period="2026-03-15", usd=Decimal("4.2"), requests=99)
        spent.roll_if_needed("2026-03-16")
        assert spent.usd == Decimal(0)
        assert spent.requests == 0
        assert spent.period == "2026-03-16"


# =============================================================================
# 规则校验
# =============================================================================


@pytest.fixture
def session(tmp_path: Path):
    db = tmp_path / "q.db"
    migrate.upgrade_to_head(db, tmp_path / "b")
    engine = make_engine(db)
    maker = make_sessionmaker(engine)
    yield maker
    asyncio.run(engine.dispose())


class TestRuleValidation:
    def _upsert(self, maker, **kw):
        async def go():
            async with maker() as s:
                await quota_svc.upsert(s, **kw)
                await s.commit()

        return asyncio.run(go())

    def test_needs_at_least_one_limit(self, session):
        """两个上限都空等于没有配额——那种规则存进去只会让人误以为设了。"""
        with pytest.raises(quota_svc.InvalidQuota, match="至少要设一个"):
            self._upsert(
                session,
                subject_type="user",
                subject_id=1,
                window="day",
                limit_usd=None,
                limit_requests=None,
            )

    @pytest.mark.parametrize(
        ("field", "value"),
        [("limit_usd", Decimal("0")), ("limit_usd", Decimal("-1")), ("limit_requests", 0)],
    )
    def test_rejects_non_positive(self, session, field: str, value):
        kw = {
            "subject_type": "user",
            "subject_id": 1,
            "window": "day",
            "limit_usd": None,
            "limit_requests": None,
            field: value,
        }
        with pytest.raises(quota_svc.InvalidQuota):
            self._upsert(session, **kw)

    def test_rejects_unknown_subject_or_window(self, session):
        for kw in (
            {"subject_type": "nope", "window": "day"},
            {"subject_type": "user", "window": "hour"},
        ):
            with pytest.raises(quota_svc.InvalidQuota):
                self._upsert(
                    session, subject_id=1, limit_usd=Decimal("1"), limit_requests=None, **kw
                )

    def test_upsert_replaces(self, session):
        for usd in ("1", "9"):
            self._upsert(
                session,
                subject_type="user",
                subject_id=1,
                window="day",
                limit_usd=Decimal(usd),
                limit_requests=None,
            )

        async def go():
            async with session() as s:
                return await quota_svc.list_rules(s)

        rules = asyncio.run(go())
        assert len(rules) == 1, "同一主体同一窗口只该有一条"
        assert rules[0].limit_usd == "9"


# =============================================================================
# 检查与累加
# =============================================================================


class TestCheckAndRecord:
    def _svc(self, maker, rules: list[dict]) -> QuotaService:
        async def go() -> QuotaService:
            async with maker() as s:
                for r in rules:
                    await quota_svc.upsert(s, **r)
                await s.commit()
            svc = QuotaService(maker)
            await svc.reload()
            return svc

        return asyncio.run(go())

    def test_no_rules_means_no_limit(self, session):
        svc = self._svc(session, [])
        for _ in range(100):
            svc.reserve(user_id=1, token_id=1, agent_id=1).settle(Decimal("999"))

    def test_request_limit_blocks(self, session):
        svc = self._svc(
            session,
            [
                {
                    "subject_type": "user",
                    "subject_id": 1,
                    "window": "day",
                    "limit_usd": None,
                    "limit_requests": 3,
                }
            ],
        )
        for _ in range(3):
            svc.reserve(user_id=1, token_id=None, agent_id=None).settle(None)
        with pytest.raises(QuotaExceeded) as e:
            svc.reserve(user_id=1, token_id=None, agent_id=None)
        assert e.value.extra["window"] == "day"
        assert e.value.extra["limit_kind"] == "requests"

    def test_usd_limit_blocks(self, session):
        svc = self._svc(
            session,
            [
                {
                    "subject_type": "user",
                    "subject_id": 1,
                    "window": "month",
                    "limit_usd": Decimal("0.05"),
                    "limit_requests": None,
                }
            ],
        )
        svc.reserve(user_id=1, token_id=None, agent_id=None).settle(Decimal("0.049"))
        svc.reserve(user_id=1, token_id=None, agent_id=None)  # 还差一点
        svc.reserve(user_id=1, token_id=None, agent_id=None).settle(Decimal("0.002"))
        with pytest.raises(QuotaExceeded):
            svc.reserve(user_id=1, token_id=None, agent_id=None)

    def test_tightest_rule_wins(self, session):
        """**最紧的那条先生效**，不是取最宽松的、也不是只看用户级。

        「给某个 token 单独设一个小额度」这种用法要成立，就必须每一级都真的拦。
        """
        svc = self._svc(
            session,
            [
                {
                    "subject_type": "user",
                    "subject_id": 1,
                    "window": "day",
                    "limit_usd": None,
                    "limit_requests": 1000,
                },
                {
                    "subject_type": "token",
                    "subject_id": 7,
                    "window": "day",
                    "limit_usd": None,
                    "limit_requests": 2,
                },
            ],
        )
        for _ in range(2):
            svc.reserve(user_id=1, token_id=7, agent_id=None).settle(None)
        with pytest.raises(QuotaExceeded) as e:
            svc.reserve(user_id=1, token_id=7, agent_id=None)
        assert e.value.extra["subject_type"] == "token", "该是 token 级先拦住"

        # 另一把 token 不受影响
        svc.reserve(user_id=1, token_id=8, agent_id=None)

    def test_agent_level_limit(self, session):
        svc = self._svc(
            session,
            [
                {
                    "subject_type": "agent",
                    "subject_id": 5,
                    "window": "total",
                    "limit_usd": None,
                    "limit_requests": 1,
                }
            ],
        )
        svc.reserve(user_id=1, token_id=1, agent_id=5).settle(None)
        with pytest.raises(QuotaExceeded):
            svc.reserve(user_id=1, token_id=1, agent_id=5)
        svc.reserve(user_id=1, token_id=1, agent_id=6)  # 别的 Agent 不受影响

    def test_unpriced_calls_still_count_requests(self, session):
        """约三分之一的在售模型查不到价，此时 cost 是 None。

        金额上限对它们无能为力——所以次数上限才是永远可执行的那道兜底。
        """
        svc = self._svc(
            session,
            [
                {
                    "subject_type": "user",
                    "subject_id": 1,
                    "window": "day",
                    "limit_usd": Decimal("100"),
                    "limit_requests": 2,
                }
            ],
        )
        for _ in range(2):
            svc.reserve(user_id=1, token_id=None, agent_id=None).settle(None)
        with pytest.raises(QuotaExceeded) as e:
            svc.reserve(user_id=1, token_id=None, agent_id=None)
        assert e.value.extra["limit_kind"] == "requests"


# =============================================================================
# 重启不归零
# =============================================================================


class TestSurvivesRestart:
    def test_seeds_spent_from_db(self, session, tmp_path: Path):
        """**不播种的话每次重启配额都会归零**——而重启就是这个项目的升级方式，
        等于配额形同虚设。
        """
        from xingcha.db.models import Run, RunUsage, utcnow

        async def go() -> QuotaService:
            async with session() as s:
                await quota_svc.upsert(
                    s,
                    subject_type="user",
                    subject_id=1,
                    window="day",
                    limit_usd=Decimal("1"),
                    limit_requests=None,
                )
                # 模拟"重启之前已经花掉的钱"
                for i in range(3):
                    rid = f"seed{i}"
                    s.add(
                        Run(
                            id=rid,
                            kind="agent",
                            user_id=1,
                            model="m",
                            status="ok",
                            started_at=utcnow(),
                        )
                    )
                    s.add(RunUsage(run_id=rid, model="m", cost_usd="0.30", cost_source="unknown"))
                await s.commit()

            svc = QuotaService(session)
            await svc.reload()
            return svc

        svc = asyncio.run(go())
        snap = svc.snapshot()[0]
        assert snap["spent_requests"] == 3
        assert Decimal(str(snap["spent_usd"])) == Decimal("0.90")
        # 已经花了 0.9，上限 1 —— 再来一次就该被拦
        svc.reserve(user_id=1, token_id=None, agent_id=None).settle(Decimal("0.2"))
        with pytest.raises(QuotaExceeded):
            svc.reserve(user_id=1, token_id=None, agent_id=None)


# =============================================================================
# 端到端：配额真的拦住 HTTP 调用
# =============================================================================


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
                description=None,
                instructions="抽取。",
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
                limit_usd=None,
                limit_requests=2,
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


class TestEndToEnd:
    def test_third_call_is_429_and_upstream_untouched(self, wired, upstream: FakeUpstream):
        """超限必须在**打上游之前**拦住。

        Agent 路径最坏会调用 1+retries 次模型，先花钱再拦等于没拦。
        """
        client, token = wired
        upstream.tool_payloads = [GOOD]

        for _ in range(2):
            r = client.post(
                "/v1/chat/completions",
                json={"model": "extract", "messages": [{"role": "user", "content": "x"}]},
                headers=auth(token),
            )
            assert r.status_code == 200

        upstream.reset()
        r = client.post(
            "/v1/chat/completions",
            json={"model": "extract", "messages": [{"role": "user", "content": "x"}]},
            headers=auth(token),
        )
        assert r.status_code == 429
        body = r.json()["error"]
        assert body["type"] == C.ErrorType.QUOTA_EXCEEDED.value
        assert body["subject_type"] == "user"
        assert body["window"] == "day"
        assert upstream.hit_count == 0, "超限的请求不该打到上游"

    def test_counts_are_not_delayed_by_the_usage_buffer(self, wired, upstream: FakeUpstream):
        """**这条是配额准确性的核心。**

        用量是异步批量落库的（5 秒或 50 条才 flush）。如果配额从数据库读求和，
        那么这两次调用在 flush 之前都还没落盘，第三次就不会被拦——在一次突发里
        能漏掉整批。所以内存计数才是权威。
        """
        client, token = wired
        upstream.tool_payloads = [GOOD]

        # 连着打三次，中间不给 buffer 任何 flush 的机会
        codes = []
        for _ in range(3):
            r = client.post(
                "/v1/chat/completions",
                json={"model": "extract", "messages": [{"role": "user", "content": "x"}]},
                headers=auth(token),
            )
            codes.append(r.status_code)
        assert codes == [200, 200, 429]

    def test_failed_calls_count_too(self, settings: Settings, upstream: FakeUpstream):
        """**一次重试耗尽的调用照样花了钱**（1+retries 次模型调用）。

        不计入配额等于让失败的调用免费，而那恰好是最贵的一类。
        """
        settings.ensure_data_dir()
        migrate.upgrade_to_head(settings.db_path, settings.backup_dir)
        keyring = Keyring.load_or_create(settings.secret_path)

        async def seed() -> str:
            engine = make_engine(settings.db_path)
            maker = make_sessionmaker(engine)
            async with maker() as s:
                await setting_svc.set_(
                    s, keyring, C.SETTING_KEY_OPENROUTER_API_KEY, "sk-or-v1-fake"
                )
                await setting_svc.set_(
                    s, keyring, C.SETTING_KEY_OPENROUTER_BASE_URL, upstream.base_url
                )
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
                await quota_svc.upsert(
                    s,
                    subject_type="user",
                    subject_id=1,
                    window="day",
                    limit_usd=None,
                    limit_requests=1,
                )
                tok = await auth_svc.issue(s, name="t")
                await s.commit()
            await engine.dispose()
            return tok.plaintext

        token = asyncio.run(seed())
        upstream.reset()
        upstream.tool_payloads = [{"nope": 1}]  # 持续违规

        with TestClient(create_app(settings)) as client:
            first = client.post(
                "/v1/chat/completions",
                json={"model": "extract", "messages": [{"role": "user", "content": "x"}]},
                headers=auth(token),
            )
            assert first.status_code == 422  # 重试耗尽

            second = client.post(
                "/v1/chat/completions",
                json={"model": "extract", "messages": [{"role": "user", "content": "x"}]},
                headers=auth(token),
            )
        assert second.status_code == 429, "失败的那次也该占用配额"


class TestPassthroughRespectsTheContract:
    """契约 §3.9 冻结了「直通层不执行配额」。

    打开它是一次**收紧**，所以必须是显式开关 + 能力位公布，而不是升级的副作用。
    """

    def test_off_by_default(self, wired, upstream: FakeUpstream):
        client, token = wired
        upstream.reset()
        # 用户级配额是 2 次，但直通不受它约束
        codes = [
            client.post(
                "/v1/chat/completions",
                json={"model": "openai/gpt-5", "messages": [{"role": "user", "content": "x"}]},
                headers=auth(token),
            ).status_code
            for _ in range(4)
        ]
        assert codes == [200, 200, 200, 200]

    def test_feature_bit_appears_only_when_enabled(self, settings: Settings):
        with TestClient(create_app(settings)) as c:
            assert C.FEATURE_QUOTA_PASSTHROUGH not in c.get("/version").json()["features"]

        settings2 = Settings(data_dir=settings.data_dir, quota_on_passthrough=True)
        with TestClient(create_app(settings2)) as c:
            assert C.FEATURE_QUOTA_PASSTHROUGH in c.get("/version").json()["features"]


# =============================================================================
# 并发不穿透 —— 计划里那条验收
# =============================================================================


class TestNoLeakUnderConcurrency:
    """**这是配额最容易被写错的地方。**

    ``check`` 与真正的计数之间隔着模型调用（一个 await）。如果计数放在调用之后，
    50 个并发请求会全部通过检查、然后才各自 +1——限额 2 会放过 50 个。

    所以次数在检查时就占掉。这两条测试锁死这个行为。
    """

    def test_fifty_concurrent_reserves_respect_the_limit(self, session):
        """纯并发压 reserve：允许的次数必须恰好等于限额，一个不多。"""

        async def go() -> tuple[int, int]:
            async with session() as s:
                await quota_svc.upsert(
                    s,
                    subject_type="user",
                    subject_id=1,
                    window="day",
                    limit_usd=None,
                    limit_requests=5,
                )
                await s.commit()
            svc = QuotaService(session)
            await svc.reload()

            allowed = 0
            rejected = 0

            async def one() -> None:
                nonlocal allowed, rejected
                try:
                    r = svc.reserve(user_id=1, token_id=None, agent_id=None)
                except QuotaExceeded:
                    rejected += 1
                    return
                allowed += 1
                # 模拟模型调用：这个 await 正是穿透发生的窗口
                await asyncio.sleep(0.001)
                r.settle(Decimal("0.01"))

            await asyncio.gather(*(one() for _ in range(50)))
            return allowed, rejected

        allowed, rejected = asyncio.run(go())
        assert allowed == 5, f"限额 5 却放过了 {allowed} 个 —— 配额穿透了"
        assert rejected == 45

    def test_fifty_concurrent_http_calls(self, wired, upstream: FakeUpstream):
        """端到端：50 个并发 HTTP 请求，限额 2。

        断言的不只是"有 429"，而是**上游恰好被打了 2 次**——配额穿透的表现就是
        上游被打的次数超过限额，而那是真金白银。
        """
        client, token = wired
        upstream.reset()
        upstream.tool_payloads = [GOOD]

        import concurrent.futures

        def call() -> int:
            return client.post(
                "/v1/chat/completions",
                json={"model": "extract", "messages": [{"role": "user", "content": "x"}]},
                headers=auth(token),
            ).status_code

        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            codes = list(pool.map(lambda _: call(), range(50)))

        assert codes.count(200) == 2, f"限额 2 却成功了 {codes.count(200)} 次"
        assert codes.count(429) == 48
        assert upstream.hit_count == 2, f"上游被打了 {upstream.hit_count} 次，应当只有 2 次"


class TestReservationRelease:
    def test_early_rejection_does_not_eat_a_slot(self, settings: Settings, upstream: FakeUpstream):
        """`stream_unsupported` 这类早期拒绝根本没打到上游、没花钱。

        如果它也吃掉一个名额，那么一个反复用错参数的客户端会把配额耗光，
        而实际一分钱没花。
        """
        settings.ensure_data_dir()
        migrate.upgrade_to_head(settings.db_path, settings.backup_dir)
        keyring = Keyring.load_or_create(settings.secret_path)

        async def seed() -> str:
            engine = make_engine(settings.db_path)
            maker = make_sessionmaker(engine)
            async with maker() as s:
                await setting_svc.set_(
                    s, keyring, C.SETTING_KEY_OPENROUTER_API_KEY, "sk-or-v1-fake"
                )
                await setting_svc.set_(
                    s, keyring, C.SETTING_KEY_OPENROUTER_BASE_URL, upstream.base_url
                )
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
                await quota_svc.upsert(
                    s,
                    subject_type="user",
                    subject_id=1,
                    window="day",
                    limit_usd=None,
                    limit_requests=1,
                )
                tok = await auth_svc.issue(s, name="t")
                await s.commit()
            await engine.dispose()
            return tok.plaintext

        token = asyncio.run(seed())
        upstream.reset()
        upstream.tool_payloads = [GOOD]

        with TestClient(create_app(settings)) as client:
            # 先来三次注定被早期拒绝的（结构化 Agent + stream）
            for _ in range(3):
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

            # 名额应当还在
            ok = client.post(
                "/v1/chat/completions",
                json={"model": "extract", "messages": [{"role": "user", "content": "x"}]},
                headers=auth(token),
            )
        assert ok.status_code == 200, "早期拒绝吃掉了名额"
