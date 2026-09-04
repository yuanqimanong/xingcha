"""OTel trace。

------------------------------------------------------------------------------
这一层要守住什么
------------------------------------------------------------------------------

管理面的运行列表回答"跑了几次、花了多少、有没有报错"。它回答不了**"模型到底看到了
什么、又吐回了什么"**——而调 Agent 的时间几乎全花在这个问题上。

所以这里断言的不是"有没有 span"，而是三件具体的事：

1. **默认关**。打开意味着提示词与模型输出会离开这台机器，必须是显式决定；
2. **装配失败不影响服务**。endpoint 写错、Langfuse 挂了，只能降级成"没有 trace"；
3. **span 带得回 run_id**，否则 trace 与运行记录两边对不上，排查时只能靠时间戳猜。

用的是真的 ``TracerProvider`` + 内存 exporter，不是 mock —— 要验证的恰恰是
"pydantic-ai 的埋点真的接到了我们的 provider 上"，而 mock 掉 provider 就把这件事
一起 mock 掉了。
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
from xingcha.obs import tracing as tracing_mod
from xingcha.services import agent as agent_svc
from xingcha.services import auth as auth_svc
from xingcha.services import setting as setting_svc

SCHEMA = {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]}
GOOD = {"title": "看得见"}


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def seed(settings: Settings, base_url: str, *, trace_endpoint: str | None = None) -> str:
    settings.ensure_data_dir()
    migrate.upgrade_to_head(settings.db_path, settings.backup_dir)
    keyring = Keyring.load_or_create(settings.secret_path)

    async def go() -> str:
        engine = make_engine(settings.db_path)
        maker = make_sessionmaker(engine)
        async with maker() as s:
            await setting_svc.set_(s, keyring, C.SETTING_KEY_OPENROUTER_API_KEY, "sk-or-v1-fake")
            await setting_svc.set_(s, keyring, C.SETTING_KEY_OPENROUTER_BASE_URL, base_url)
            if trace_endpoint is not None:
                await setting_svc.set_(s, keyring, C.SETTING_KEY_TRACE_ENDPOINT, trace_endpoint)
            for slug, schema, tier in (("extract", SCHEMA, Tier.T2), ("chat", None, None)):
                await agent_svc.save(
                    s,
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
            tok = await auth_svc.issue(s, name="t")
            await s.commit()
        await engine.dispose()
        return tok.plaintext

    return asyncio.run(go())


@pytest.fixture
def memory_tracing() -> Iterator[tuple[tracing_mod.Tracing, list]]:
    """一个真的 TracerProvider，span 收进内存。

    ``setup()`` 里那条 OTLP exporter 换成内存的，其余（provider、processor、
    ``instrument_all`` 接线）全都是生产那一套。
    """
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "xingcha-test"}))
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    tracing = tracing_mod.Tracing(provider, processor, endpoint="memory://", include_content=True)
    yield tracing, exporter.get_finished_spans  # type: ignore[misc]
    tracing.shutdown()


# =============================================================================
# 默认关
# =============================================================================


class TestOffByDefault:
    def test_no_trace_config_means_no_tracing(self, settings: Settings, upstream: FakeUpstream):
        """**默认关。** 没配 endpoint 就一个 span 都不发。

        打开意味着提示词与模型输出离开这台机器，而这个项目存在的理由恰恰是不想让
        请求经过别人手里。所以它必须是一次显式的决定，不能是升级的副作用。
        """
        seed(settings, upstream.base_url)
        upstream.reset()
        with TestClient(create_app(settings)) as client:
            assert client.app.state.xc.tracing is None  # type: ignore[attr-defined]
            r = client.get("/readyz")
            assert r.json()["checks"]["trace"] == "off"

    def test_readyz_says_whether_content_is_included(
        self, settings: Settings, upstream: FakeUpstream
    ):
        """ "含不含内容"必须能查到——它关系到提示词有没有离开这台机器。"""
        seed(settings, upstream.base_url, trace_endpoint="http://127.0.0.1:9/v1/traces")
        upstream.reset()
        with TestClient(create_app(settings)) as client:
            checks = client.get("/readyz").json()["checks"]
        assert checks["trace"] == "on"
        assert checks["trace_includes_content"] is True
        assert checks["trace_endpoint"] == "http://127.0.0.1:9/v1/traces"


# =============================================================================
# 装配失败不影响服务
# =============================================================================


class TestSetupNeverBreaksTheService:
    def test_a_black_hole_endpoint_still_serves(self, settings: Settings, upstream: FakeUpstream):
        """endpoint 指向黑洞时，服务照常工作。

        BatchSpanProcessor 在后台线程发，发不出去是它自己的事。这条要是不成立，
        一次 Langfuse 宕机就会变成星槎宕机——用可观测换可用性是明显不划算的交易。
        """
        token = seed(settings, upstream.base_url, trace_endpoint="http://127.0.0.1:9/v1/traces")
        upstream.reset()
        upstream.tool_payloads = [GOOD]

        with TestClient(create_app(settings)) as client:
            r = client.post(
                "/v1/chat/completions",
                json={"model": "extract", "messages": [{"role": "user", "content": "x"}]},
                headers=auth(token),
            )
        assert r.status_code == 200
        assert json.loads(r.json()["choices"][0]["message"]["content"]) == GOOD

    def test_no_endpoint_means_no_tracing(self):
        assert tracing_mod.setup(endpoint="") is None

    def test_setup_returns_none_instead_of_raising(self, monkeypatch: pytest.MonkeyPatch):
        """装配过程炸掉时返回 None，**不抛**。

        抛的话一个写错的 endpoint 就变成"服务起不来"，而它本来只该是"看不到 trace"。
        用 monkeypatch 造这个失败而不是找一个恰好能让 exporter 构造失败的字符串：
        后者依赖 OTel 的输入校验有多严，而那随版本变——测出来的会是"这个版本恰好
        宽容"，不是我们的守卫对不对。
        """
        import opentelemetry.exporter.otlp.proto.http.trace_exporter as mod

        def boom(*a, **kw):
            raise RuntimeError("exporter 起不来")

        monkeypatch.setattr(mod, "OTLPSpanExporter", boom)
        assert tracing_mod.setup(endpoint="https://example.invalid/v1/traces") is None

    def test_shutdown_survives_a_dead_endpoint(self, settings: Settings, upstream: FakeUpstream):
        """关停 flush 打不通也不能让关停失败。

        否则一次 Langfuse 宕机会卡住容器的优雅停机，而停机卡住之后被 SIGKILL，
        用量缓冲那批 run 行就一起丢了——可观测的故障传染成了账单的故障。
        """
        seed(settings, upstream.base_url, trace_endpoint="http://127.0.0.1:9/v1/traces")
        upstream.reset()
        with TestClient(create_app(settings)):
            pass  # 退出即触发关停


# =============================================================================
# span 内容
# =============================================================================


class TestSpansCarryTheRunId:
    def _run(self, settings: Settings, upstream: FakeUpstream, tracing, **payload):
        token = seed(settings, upstream.base_url)
        upstream.reset()
        upstream.tool_payloads = [GOOD]
        app = create_app(settings)
        with TestClient(app) as client:
            # 启动时没配 endpoint，所以这里手动注入内存 provider——
            # 换掉的只是 exporter，接线仍然是生产那一套
            from xingcha.core import builder

            app.state.xc.tracing = tracing
            builder.enable_instrumentation(tracing)
            app.state.xc.runtimes.clear()  # 让 Agent 用装好埋点的模型重建
            try:
                return client.post(
                    "/v1/chat/completions",
                    json={"messages": [{"role": "user", "content": "x"}], **payload},
                    headers=auth(token),
                )
            finally:
                builder.enable_instrumentation(None)

    def test_agent_run_span_has_run_id_and_cost(
        self, settings: Settings, upstream: FakeUpstream, memory_tracing
    ):
        """trace 必须带得回 run_id。

        对不上的话，"客户端报了个 run_id，去 trace 里查"这条排查路径直接断掉——
        而那是这套东西最主要的用法。
        """
        tracing, spans = memory_tracing
        r = self._run(settings, upstream, tracing, model="extract")
        assert r.status_code == 200
        run_id = r.json()[C.EXT_KEY]["run_id"]

        ours = [s for s in spans() if s.name == "xingcha.agent"]
        assert ours, f"没有 xingcha.agent span，只有 {[s.name for s in spans()]}"
        attrs = ours[-1].attributes
        assert attrs[tracing_mod.ATTR_RUN_ID] == run_id
        assert attrs[tracing_mod.ATTR_MODEL] == "extract"
        assert attrs[tracing_mod.ATTR_STATUS] == C.RunStatus.OK.value
        assert attrs[tracing_mod.ATTR_TIER] == Tier.T2.value
        assert attrs[tracing_mod.ATTR_COST_SOURCE] in {s.value for s in C.CostSource}

    def test_pydantic_ai_spans_nest_under_ours(
        self, settings: Settings, upstream: FakeUpstream, memory_tracing
    ):
        """pydantic-ai 的 span 必须挂在我们那个下面。

        这是"词法作用域开 span"换来的东西：不嵌套的话，Langfuse 里会看到两棵互不
        相关的树，一棵有 run_id 一棵有提示词，而你需要的是同一棵上的两者。
        """
        tracing, spans = memory_tracing
        self._run(settings, upstream, tracing, model="extract")

        got = spans()
        ours = next(s for s in got if s.name == "xingcha.agent")
        children = [
            s for s in got if s.parent is not None and s.parent.span_id == ours.context.span_id
        ]
        assert children, f"pydantic-ai 的 span 没挂上来，收到的是 {[s.name for s in got]}"

    def test_streaming_span_covers_the_whole_stream(
        self, settings: Settings, upstream: FakeUpstream, memory_tracing
    ):
        """流式的 span 要包住整条流，而不是在第一帧就关掉。

        在 API 层包 StreamingResponse 的话，span 会在第一帧发出去时结束，之后所有
        delta 与最终的费用全落在 span 外面——看板上表现为"流式调用都是 0 token"。
        """
        tracing, spans = memory_tracing
        token = seed(settings, upstream.base_url)
        upstream.reset()
        upstream.stream_chunks = ["a", "b", "c"]

        app = create_app(settings)
        from xingcha.core import builder

        with TestClient(app) as client:
            app.state.xc.tracing = tracing
            builder.enable_instrumentation(tracing)
            app.state.xc.runtimes.clear()
            try:
                with client.stream(
                    "POST",
                    "/v1/chat/completions",
                    json={
                        "model": "chat",
                        "messages": [{"role": "user", "content": "x"}],
                        "stream": True,
                    },
                    headers=auth(token),
                ) as r:
                    raw = b"".join(r.iter_bytes()).decode()
            finally:
                builder.enable_instrumentation(None)

        assert raw.endswith(C.SSE_DONE)
        ours = [s for s in spans() if s.name == "xingcha.agent"]
        assert ours
        attrs = ours[-1].attributes
        assert attrs[tracing_mod.ATTR_STATUS] == C.RunStatus.OK.value
        assert attrs[tracing_mod.ATTR_MODEL] == "chat"


# =============================================================================
# Langfuse 凭据
# =============================================================================


class TestLangfuseCredentials:
    def test_basic_auth_header_is_built_for_you(self):
        """让人手算 base64 再贴进配置，是一个必然出错、且错了只表现为
        "trace 没上去"的步骤。"""
        import base64

        h = tracing_mod.langfuse_headers("pk-lf-1", "sk-lf-2")
        assert h["Authorization"].startswith("Basic ")
        raw = base64.b64decode(h["Authorization"].removeprefix("Basic ")).decode()
        assert raw == "pk-lf-1:sk-lf-2"

    def test_secret_key_is_stored_encrypted(self, settings: Settings):
        """Langfuse 的 secret key 与上游 key 同级：**不能是环境变量**。

        环境变量会出现在 docker inspect 与 /proc/<pid>/environ 里。
        """
        assert C.SETTING_KEY_TRACE_SECRET_KEY in setting_svc.SECRET_KEYS
        assert C.SETTING_KEY_TRACE_ENDPOINT in setting_svc.KNOWN_KEYS
        assert C.SETTING_KEY_TRACE_PUBLIC_KEY in setting_svc.KNOWN_KEYS
