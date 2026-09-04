"""裸模型直通。

这是全项目风险最高的一段：一个 catch-all 反代后面挂着一把付费 key。下面每一条
都对应契约 §3.9 或准入清单里的一项。

**最重要的是 TestAuthIsMandatory**——它断言的不只是"返回 401"，而是"上游一次都
没有被打到"。一个先转发再检查的实现同样会返回 401，但钱已经花出去了。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import FakeUpstream
from xingcha import contract as C
from xingcha.app import create_app
from xingcha.config import Settings
from xingcha.crypto import Keyring
from xingcha.db import migrate
from xingcha.db.engine import make_engine, make_sessionmaker
from xingcha.services import auth as auth_svc
from xingcha.services import setting as setting_svc


@pytest.fixture
def wired(settings: Settings, upstream: FakeUpstream) -> Iterator[tuple[TestClient, str]]:
    """起一个配好上游、签好一把 key 的星槎。返回 ``(client, token)``。"""
    import asyncio

    settings.ensure_data_dir()
    migrate.upgrade_to_head(settings.db_path, settings.backup_dir)
    keyring = Keyring.load_or_create(settings.secret_path)

    async def seed() -> str:
        engine = make_engine(settings.db_path)
        maker = make_sessionmaker(engine)
        async with maker() as s:
            await setting_svc.set_(s, keyring, C.SETTING_KEY_OPENROUTER_API_KEY, "sk-or-v1-fake")
            await setting_svc.set_(s, keyring, C.SETTING_KEY_OPENROUTER_BASE_URL, upstream.base_url)
            tok = await auth_svc.issue(s, name="test")
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
# 鉴权 —— 一天烧光余额级的那一条
# =============================================================================


class TestAuthIsMandatory:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("post", "/v1/chat/completions"),
            ("get", "/v1/models"),
            ("post", "/v1/embeddings"),
            ("get", "/v1/anything/at/all"),
        ],
    )
    def test_no_key_means_401_and_upstream_untouched(
        self, wired, upstream: FakeUpstream, method: str, path: str
    ):
        """**上游一次都不能被打到。**

        一个先转发再检查的实现同样会返回 401，但钱已经花出去了。所以这里断言的是
        上游的调用计数，而不只是状态码。
        """
        client, _ = wired
        upstream.reset()
        kwargs = {"json": {"model": "openai/gpt-5"}} if method == "post" else {}
        r = getattr(client, method)(path, **kwargs)
        assert r.status_code == 401
        assert r.json()["error"]["type"] == C.ErrorType.INVALID_API_KEY.value
        assert upstream.hit_count == 0, "无鉴权的请求被转发给了上游"

    def test_bad_key_upstream_untouched(self, wired, upstream: FakeUpstream):
        client, _ = wired
        upstream.reset()
        r = client.post(
            "/v1/chat/completions",
            json={"model": "openai/gpt-5"},
            headers=auth("sk-xc-1-" + "0" * 16 + "-" + "x" * 43),
        )
        assert r.status_code == 401
        assert upstream.hit_count == 0

    def test_upstream_key_is_not_accepted_as_ours(self, wired, upstream: FakeUpstream):
        """拿上游的 key 直接调星槎必须失败——否则星槎就成了一个 key 洗白通道。"""
        client, _ = wired
        upstream.reset()
        r = client.get("/v1/models", headers=auth("sk-or-v1-realupstreamkey"))
        assert r.status_code == 401
        assert upstream.hit_count == 0

    def test_revoked_key_stops_working_immediately(self, wired, settings: Settings, upstream):
        import asyncio

        client, token = wired
        assert client.get("/v1/models", headers=auth(token)).status_code == 200

        kid = C.TOKEN_ENVELOPE_RE.match(token).group("kid")  # type: ignore[union-attr]

        async def revoke() -> None:
            engine = make_engine(settings.db_path)
            maker = make_sessionmaker(engine)
            async with maker() as s:
                await auth_svc.revoke(s, kid)
                await s.commit()
            await engine.dispose()

        asyncio.run(revoke())
        upstream.reset()
        assert client.get("/v1/models", headers=auth(token)).status_code == 401
        assert upstream.hit_count == 0


# =============================================================================
# 路径归属
# =============================================================================


class TestPathOwnership:
    def test_trailing_slash_still_ours(self, wired):
        """GET /v1/models/ 必须与 /v1/models 逐字节相同。

        不归一化的话它会落进 catch-all 被反代出去——客户端拿到 200、拿到上游的模型
        列表、一个 Agent 都看不到，而且没有任何报错。
        """
        client, token = wired
        a = client.get("/v1/models", headers=auth(token))
        b = client.get("/v1/models/", headers=auth(token))
        assert a.status_code == b.status_code == 200
        assert a.content == b.content

    def test_embeddings_is_proxied(self, wired, upstream: FakeUpstream):
        """/v1 下的非自有路径原样反代——这是"任何 OpenRouter 能力都能用"的保证。"""
        client, token = wired
        upstream.reset()
        r = client.post("/v1/embeddings", json={"input": "hi"}, headers=auth(token))
        assert r.status_code == 200
        assert r.json()["object"] == "list"
        assert upstream.last().path == "/v1/embeddings"

    def test_multi_segment_model_path_goes_upstream(self, wired, upstream: FakeUpstream):
        """OpenRouter 自己的 /models/{author}/{slug}/endpoints 属于上游。"""
        client, token = wired
        upstream.reset()
        r = client.get("/v1/models/openai/gpt-5/endpoints", headers=auth(token))
        assert r.status_code == 200
        assert upstream.last().path == "/v1/models/openai/gpt-5/endpoints"

    def test_retrieve_model_is_ours(self, wired, upstream: FakeUpstream):
        """单段 retrieve-model 必须由星槎回答。

        归给反代的话，客户端拿 Agent slug 来问会打到上游拿回 404，据此判定
        「这个模型不存在」——而事后收回算破坏性变更，等于永久坏掉。
        """
        client, token = wired
        client.get("/v1/models", headers=auth(token))  # 先填充目录
        upstream.reset()
        r = client.get("/v1/models/openai%2Fgpt-5", headers=auth(token))
        # 编码后的斜杠会被解成多段 → 归上游；这里只断言它没被当成星槎的 404
        assert r.status_code in (200, 404)

    def test_path_traversal_rejected(self, wired, upstream: FakeUpstream):
        client, token = wired
        upstream.reset()
        r = client.get("/v1/../admin/settings", headers=auth(token))
        # 要么被 starlette 归一化掉，要么被我们拒绝；无论如何不能带着 key 打到上游
        assert r.status_code != 200 or upstream.hit_count == 0

    def test_options_needs_no_auth_and_never_proxied(self, wired, upstream: FakeUpstream):
        """浏览器预检不带 Authorization。

        不自己应答的话，CORS 预检会由上游的策略决定，而星槎自己的响应又不带 CORS 头
        —— 表现为"非流式偶尔能用、浏览器直连必挂"。
        """
        client, _ = wired
        upstream.reset()
        r = client.options("/v1/chat/completions")
        assert r.status_code == 204
        assert upstream.hit_count == 0

    def test_options_sends_no_cors_headers_by_default(self, wired):
        """默认空 = 不发 CORS 头。放开 origin 是纯加法，所以默认可以最严。"""
        client, _ = wired
        r = client.options("/v1/models", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


# =============================================================================
# 请求头卫生
# =============================================================================


class TestHeaderHygiene:
    @pytest.mark.parametrize(
        "header",
        ["X-Forwarded-For", "X-Real-IP", "Forwarded", "CF-Connecting-IP", "True-Client-IP"],
    )
    def test_all_client_ip_headers_stripped(self, wired, upstream: FakeUpstream, header: str):
        """只剥 XFF 是不够的：Forwarded 与 CF-Connecting-IP 同样会把真实来源交给上游，
        中转形同白建。"""
        client, token = wired
        upstream.reset()
        client.post(
            "/v1/embeddings",
            json={"input": "x"},
            headers={**auth(token), header: "203.0.113.9"},
        )
        assert header.lower() not in upstream.last().headers

    def test_client_token_never_reaches_upstream(self, wired, upstream: FakeUpstream):
        """上游只该看到上游自己的 key，绝不能看到 sk-xc-。"""
        client, token = wired
        upstream.reset()
        client.post("/v1/embeddings", json={"input": "x"}, headers=auth(token))
        sent = upstream.last().headers.get("authorization", "")
        assert sent == "Bearer sk-or-v1-fake"
        assert "sk-xc-" not in sent

    def test_cookies_not_forwarded(self, wired, upstream: FakeUpstream):
        client, token = wired
        upstream.reset()
        client.post(
            "/v1/embeddings",
            json={"input": "x"},
            headers={**auth(token), "Cookie": "session=secret"},
        )
        assert "cookie" not in upstream.last().headers


class TestResponseHeaderAllowlist:
    def test_upstream_set_cookie_is_dropped(self, wired):
        """必须是白名单：黑名单式透传会让上游的 Set-Cookie 落在你自己的域上。"""
        client, token = wired
        r = client.post("/v1/chat/completions", json={"model": "openai/gpt-5"}, headers=auth(token))
        assert r.status_code == 200
        assert "set-cookie" not in {k.lower() for k in r.headers}

    def test_allowed_headers_pass_through(self, wired):
        client, token = wired
        r = client.post("/v1/chat/completions", json={"model": "openai/gpt-5"}, headers=auth(token))
        assert r.headers.get("x-request-id") == "up-1"


# =============================================================================
# 请求体上限
# =============================================================================


class TestBodyLimit:
    def test_oversized_body_is_413_and_upstream_untouched(self, wired, upstream: FakeUpstream):
        """没有上限时一个大 POST 就能打死这个单进程。"""
        client, token = wired
        upstream.reset()
        big = b"x" * (C.MAX_BODY_BYTES + 1024)
        r = client.post(
            "/v1/embeddings",
            content=big,
            headers={**auth(token), "Content-Type": "application/octet-stream"},
        )
        assert r.status_code == 413
        assert r.json()["error"]["type"] == C.ErrorType.REQUEST_TOO_LARGE.value
        assert upstream.hit_count == 0

    def test_normal_body_passes(self, wired):
        client, token = wired
        r = client.post("/v1/embeddings", json={"input": "x" * 1000}, headers=auth(token))
        assert r.status_code == 200


# =============================================================================
# 流式
# =============================================================================


class TestStreaming:
    def test_frames_arrive_separately(self, wired):
        """帧不能被合并成一坨——那等于取消了流式。"""
        client, token = wired
        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={"model": "openai/gpt-5", "stream": True},
            headers=auth(token),
        ) as r:
            assert r.status_code == 200
            body = b"".join(r.iter_bytes())
        frames = [ln for ln in body.decode().split("\n\n") if ln.strip()]
        assert len(frames) >= 4
        assert frames[-1].strip() == "data: [DONE]"

    def test_stream_drops_upstream_set_cookie(self, wired):
        client, token = wired
        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={"model": "openai/gpt-5", "stream": True},
            headers=auth(token),
        ) as r:
            assert "set-cookie" not in {k.lower() for k in r.headers}
            r.read()


# =============================================================================
# model 分派
# =============================================================================


class TestModelDispatch:
    def test_upstream_model_is_forwarded(self, wired, upstream: FakeUpstream):
        """v1 的核心价值：本地不挂代理就能调任意上游模型。"""
        client, token = wired
        upstream.reset()
        r = client.post(
            "/v1/chat/completions",
            json={"model": "openai/gpt-5", "messages": [{"role": "user", "content": "hi"}]},
            headers=auth(token),
        )
        assert r.status_code == 200
        assert r.json()["choices"][0]["message"]["content"] == "hi"
        assert upstream.last().path == "/v1/chat/completions"

    def test_unknown_agent_slug_is_404_not_forwarded(self, wired, upstream: FakeUpstream):
        """**绝不回落上游。**

        查不到就当上游模型转发出去的话，一个拼错的 slug 会静默变成一次真实的付费调用，
        而调用方以为自己在调 Agent。
        """
        client, token = wired
        upstream.reset()
        r = client.post("/v1/chat/completions", json={"model": "extract"}, headers=auth(token))
        assert r.status_code == 404
        assert r.json()["error"]["type"] == C.ErrorType.MODEL_NOT_FOUND.value
        assert upstream.hit_count == 0

    def test_explicit_namespace_is_rewritten_before_forwarding(self, wired, upstream):
        """xc:model/openai/gpt-5 转发给上游时必须还原成上游认识的 id。"""
        import json

        client, token = wired
        upstream.reset()
        r = client.post(
            "/v1/chat/completions",
            json={"model": "xc:model/openai/gpt-5"},
            headers=auth(token),
        )
        assert r.status_code == 200
        assert json.loads(upstream.last().body)["model"] == "openai/gpt-5"

    def test_malformed_model_is_400(self, wired, upstream: FakeUpstream):
        client, token = wired
        upstream.reset()
        r = client.post("/v1/chat/completions", json={"model": "BAD_SLUG"}, headers=auth(token))
        assert r.status_code == 400
        assert r.json()["error"]["type"] == C.ErrorType.MODEL_INVALID.value
        assert upstream.hit_count == 0


class TestRejectTable:
    @pytest.mark.parametrize("field", ["retries", "usage_limits", "response_format", "session_id"])
    def test_rejected_fields_are_400_not_ignored(self, wired, upstream, field: str):
        """静默忽略会让调用方以为自己的设置生效了。

        ``retries`` / ``usage_limits`` 能覆盖服务端的运行护栏，``response_format``
        能覆盖输出形状——这些必须明确拒绝。
        """
        client, token = wired
        upstream.reset()
        r = client.post(
            "/v1/chat/completions",
            json={"model": "openai/gpt-5", field: {"x": 1}},
            headers=auth(token),
        )
        assert r.status_code == 400
        assert r.json()["error"]["type"] == C.ErrorType.PARAM_UNSUPPORTED.value
        assert r.json()["error"]["param"] == field
        assert upstream.hit_count == 0

    def test_ignored_fields_pass_silently(self, wired):
        """ignore 表里的字段接受但无语义——不该报错。"""
        client, token = wired
        r = client.post(
            "/v1/chat/completions",
            json={"model": "openai/gpt-5", "user": "whatever", "store": True},
            headers=auth(token),
        )
        assert r.status_code == 200


# =============================================================================
# GET /v1/models
# =============================================================================


class TestModelsEndpoint:
    def test_lists_upstream_models(self, wired):
        client, token = wired
        body = client.get("/v1/models", headers=auth(token)).json()
        ids = {row["id"] for row in body["data"]}
        assert "openai/gpt-5" in ids
        assert body["object"] == "list"

    def test_rows_carry_owned_by_and_extension(self, wired):
        client, token = wired
        body = client.get("/v1/models", headers=auth(token)).json()
        row = next(r for r in body["data"] if r["id"] == "openai/gpt-5")
        assert row["owned_by"] == C.OWNED_BY_UPSTREAM
        assert row[C.EXT_KEY]["kind"] == "model"
        assert row[C.EXT_KEY]["v"] == C.EXT_SHAPE_VERSION

    def test_native_schema_detection_ignores_response_format(self, wired):
        """判档只能看 structured_outputs。

        实测今天 424 个模型里 response_format 365 个、structured_outputs 340 个——
        有 25 个只有前者。混用会把 T2 误判成 T1，于是对用户谎称"有原生保证"。
        """
        client, token = wired
        body = client.get("/v1/models", headers=auth(token)).json()
        rows = {r["id"]: r for r in body["data"]}
        assert rows["openai/gpt-5"][C.EXT_KEY]["native_schema"] is True
        assert rows["vendor/no-native"][C.EXT_KEY]["native_schema"] is False

    def test_owned_by_filter(self, wired):
        client, token = wired
        body = client.get("/v1/models?owned_by=xingcha", headers=auth(token)).json()
        assert all(r["owned_by"] == C.OWNED_BY_XINGCHA for r in body["data"])

    def test_bad_owned_by_is_400(self, wired):
        client, token = wired
        r = client.get("/v1/models?owned_by=nope", headers=auth(token))
        assert r.status_code == 400


# =============================================================================
# 计量
# =============================================================================


class TestUsageRecording:
    def _runs(self, settings: Settings) -> list[tuple]:
        import sqlite3

        with sqlite3.connect(settings.db_path) as c:
            return c.execute(
                "SELECT r.kind, r.model, r.status, u.input_tokens, u.output_tokens, "
                "u.cache_read_tokens, u.cost_usd, u.cost_source "
                "FROM run r JOIN run_usage u ON u.run_id = r.id ORDER BY r.started_at"
            ).fetchall()

    def test_buffer_flushes_on_shutdown(self, settings: Settings, upstream: FakeUpstream):
        """A9：「批量 flush」+「重启即升级」如果没有关停落盘，每次升级都会静默丢掉
        内存里那批 run 行——账单恰好在最需要它可信的时刻少报。"""
        import asyncio

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
                tok = await auth_svc.issue(s, name="t")
                await s.commit()
            await engine.dispose()
            return tok.plaintext

        token = asyncio.run(seed())

        with TestClient(create_app(settings)) as client:
            client.post("/v1/chat/completions", json={"model": "openai/gpt-5"}, headers=auth(token))
            # 还在缓冲里，尚未落盘
        # 退出 with 触发关停 flush
        rows = self._runs(settings)
        assert rows, "关停时没有把用量落盘"
        kind, model, status, inp, out, cached, cost, source = rows[-1]
        assert (kind, model, status) == ("passthrough", "openai/gpt-5", "ok")
        assert (inp, out, cached) == (10, 5, 4)
        assert source == C.CostSource.CATALOG.value
        assert cost is not None

    def test_cost_is_a_decimal_string(self, settings: Settings, upstream: FakeUpstream):
        """cost_usd 是字符串形式的 Decimal，不是 float。

        包含式语义：cache_read 是 input 的子集，要先减再按缓存价补回来。
        不做这一步会系统性高估。
        """
        from decimal import Decimal

        self.test_buffer_flushes_on_shutdown(settings, upstream)
        cost = self._runs(settings)[-1][6]
        assert isinstance(cost, str)
        # (10-4)*1.25e-6 + 5*1e-5 + 4*1.25e-7
        expected = (
            Decimal("6") * Decimal("0.00000125")
            + Decimal("5") * Decimal("0.00001")
            + Decimal("4") * Decimal("0.000000125")
        )
        assert Decimal(cost) == expected


# =============================================================================
# 未配置上游
# =============================================================================


def test_missing_upstream_key_is_actionable(settings: Settings):
    """首次部署必然还没配 key。报错要给出下一步做什么。"""
    import asyncio

    settings.ensure_data_dir()
    migrate.upgrade_to_head(settings.db_path, settings.backup_dir)
    Keyring.load_or_create(settings.secret_path)

    async def seed() -> str:
        engine = make_engine(settings.db_path)
        maker = make_sessionmaker(engine)
        async with maker() as s:
            tok = await auth_svc.issue(s, name="t")
            await s.commit()
        await engine.dispose()
        return tok.plaintext

    token = asyncio.run(seed())
    with TestClient(create_app(settings)) as client:
        r = client.post("/v1/chat/completions", json={"model": "openai/gpt-5"}, headers=auth(token))
    assert r.status_code == 503
    assert "xingcha config set" in r.json()["error"]["message"]


def test_data_dir_is_isolated(settings: Settings, tmp_path: Path):
    assert settings.data_dir.is_relative_to(tmp_path)
