"""用**真实的 openai SDK** 调星槎。

前面的测试都用 ``TestClient`` 直接打 ASGI 应用——那验证的是"我们的路由对"，
不是"OpenAI 客户端能用"。两者不一样：SDK 会按自己的 pydantic 模型校验响应，
会自己拼 URL、自己处理 SSE 分帧、自己判断该不该重试。响应里少一个 ``created``
字段，TestClient 完全不在意，SDK 会直接抛 APIError。

所以这一组起一个**真实的 HTTP 服务器**，用 ``openai.OpenAI`` 打它。
这是"业务代码只改两行"这句承诺的直接验证。

桌面客户端（Cherry Studio / Open WebUI / Continue / Cursor）装不进 CI，
它们的实测结论记在 ``docs/客户端兼容.md``，那份文档明确区分了"已验证"与"未验证"。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx2
import pytest
import uvicorn
from openai import APIStatusError, OpenAI

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


@pytest.fixture(scope="module")
def live(tmp_path_factory: pytest.TempPathFactory, upstream: FakeUpstream) -> Iterator[OpenAI]:
    """起一个真实的星槎 HTTP 服务，返回指向它的 openai 客户端。"""
    data = tmp_path_factory.mktemp("live") / "data"
    settings = Settings(data_dir=data, request_timeout=10.0, catalog_ttl_seconds=60)
    settings.ensure_data_dir()
    migrate.upgrade_to_head(settings.db_path, settings.backup_dir)
    keyring = Keyring.load_or_create(settings.secret_path)

    async def seed() -> str:
        engine = make_engine(settings.db_path)
        maker = make_sessionmaker(engine)
        async with maker() as s:
            await setting_svc.set_(s, keyring, C.SETTING_KEY_OPENROUTER_API_KEY, "sk-or-v1-fake")
            await setting_svc.set_(s, keyring, C.SETTING_KEY_OPENROUTER_BASE_URL, upstream.base_url)
            for slug, schema, tier in (
                ("extract", SCHEMA, Tier.T2),
                ("chat", None, None),
            ):
                await agent_svc.save(
                    s,
                    slug=slug,
                    name=slug,
                    description=f"{slug} agent",
                    instructions="做事。",
                    model="openai/gpt-5",
                    schema_text=json.dumps(schema) if schema else None,
                    requested_tier=tier,
                    capabilities=None,
                    retries=2,
                    native_ok=True,
                )
            tok = await auth_svc.issue(s, name="sdk")
            await s.commit()
        await engine.dispose()
        return tok.plaintext

    token = asyncio.run(seed())

    port = 8894
    config = uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(120):
        if getattr(server, "started", False):
            break
        time.sleep(0.05)
    else:  # pragma: no cover
        raise RuntimeError("星槎没能启动")

    # 这两行就是用户要改的全部
    #
    # http_client 是测试环境的需要，不是用户要写的：这台机器上有
    # ALL_PROXY=socks5://...，而 openai SDK 默认 trust_env=True，于是它自己
    # 在构造阶段就 ImportError（socksio 未装）。
    #
    # 顺带说明一件事：**任何在国内、机器上设了 socks 代理的人，用 openai SDK
    # 都会撞上这个**。而把 base_url 指向星槎之后本来就不需要代理了——
    # 那正是这个项目要解决的问题。
    client = OpenAI(
        base_url=f"http://127.0.0.1:{port}/v1",
        api_key=token,
        max_retries=0,
        http_client=httpx2.Client(trust_env=False),
    )
    yield client
    server.should_exit = True


class TestTheTwoLineChange:
    """「业务代码只改 base_url 与 api_key」——这句承诺的直接验证。"""

    def test_list_models(self, live: OpenAI):
        models = live.models.list()
        ids = {m.id for m in models.data}
        assert {"extract", "chat"} <= ids
        assert "openai/gpt-5" in ids, "上游模型也该在列表里"

    def test_agents_come_first(self, live: OpenAI):
        """部分客户端取 data[0] 当默认模型，所以顺序进了契约。"""
        first = live.models.list().data[0]
        assert first.owned_by == C.OWNED_BY_XINGCHA

    def test_retrieve_agent_by_id(self, live: OpenAI):
        """OpenAI 标准的 retrieve-model。客户端用它验证模型是否存在。"""
        m = live.models.retrieve("extract")
        assert m.id == "extract"
        assert m.owned_by == C.OWNED_BY_XINGCHA

    def test_call_upstream_model(self, live: OpenAI, upstream: FakeUpstream):
        """裸模型直通——本地不挂代理就能用任意上游模型。"""
        upstream.reset()
        r = live.chat.completions.create(
            model="openai/gpt-5", messages=[{"role": "user", "content": "你好"}]
        )
        assert r.choices[0].message.content == "hi"
        assert r.usage is not None and r.usage.total_tokens > 0

    def test_call_agent_and_parse_json(self, live: OpenAI, upstream: FakeUpstream):
        """结构化 Agent：content 是 JSON 文本，json.loads 取回。

        这是契约里"content 永远是字符串"那一条的真实后果——SDK 把它当 str 处理，
        换成 dict 会让 SDK 的响应模型校验直接失败。
        """
        upstream.reset()
        upstream.tool_payloads = [{"title": "标题", "score": 9}]
        r = live.chat.completions.create(
            model="extract", messages=[{"role": "user", "content": "文本"}]
        )
        content = r.choices[0].message.content
        assert isinstance(content, str)
        assert json.loads(content) == {"title": "标题", "score": 9}

    def test_streaming_iterates(self, live: OpenAI, upstream: FakeUpstream):
        """SDK 自己分帧、自己解析 chunk。帧形状不对它会抛，而不是静默跳过。"""
        upstream.reset()
        chunks = list(
            live.chat.completions.create(
                model="chat", messages=[{"role": "user", "content": "hi"}], stream=True
            )
        )
        assert len(chunks) >= 3
        text = "".join(c.choices[0].delta.content or "" for c in chunks if c.choices)
        assert text == "012", "上游吐的三片应当原样到达，不多不少"
        assert chunks[0].object == "chat.completion.chunk"

    def test_upstream_streaming_passthrough(self, live: OpenAI, upstream: FakeUpstream):
        upstream.reset()
        chunks = list(
            live.chat.completions.create(
                model="openai/gpt-5", messages=[{"role": "user", "content": "hi"}], stream=True
            )
        )
        assert len(chunks) >= 3


class TestErrorsReachTheSDK:
    """SDK 按 HTTP 状态码分派异常类型。错误码错了，调用方的 except 就抓不住。"""

    def test_unknown_model_is_404(self, live: OpenAI):
        with pytest.raises(APIStatusError) as e:
            live.chat.completions.create(model="nope", messages=[{"role": "user", "content": "x"}])
        assert e.value.status_code == 404

    def test_schema_violation_is_422(self, live: OpenAI, upstream: FakeUpstream):
        upstream.reset()
        upstream.tool_payloads = [{"score": "not-an-int"}]
        with pytest.raises(APIStatusError) as e:
            live.chat.completions.create(
                model="extract", messages=[{"role": "user", "content": "x"}]
            )
        assert e.value.status_code == 422
        assert "title" in str(e.value)

    def test_stream_on_structured_agent_is_400(self, live: OpenAI, upstream: FakeUpstream):
        upstream.reset()
        with pytest.raises(APIStatusError) as e:
            live.chat.completions.create(
                model="extract", messages=[{"role": "user", "content": "x"}], stream=True
            )
        assert e.value.status_code == 400

    def test_bad_key_is_401(self, live: OpenAI):
        bad = OpenAI(
            base_url=str(live.base_url),
            api_key="sk-xc-1-" + "0" * 16 + "-" + "x" * 43,
            max_retries=0,
            http_client=httpx2.Client(trust_env=False),
        )
        with pytest.raises(APIStatusError) as e:
            bad.models.list()
        assert e.value.status_code == 401


class TestExtensionsAreInvisibleToTheSDK:
    def test_x_xingcha_does_not_break_parsing(self, live: OpenAI, upstream: FakeUpstream):
        """自有字段放在 x_xingcha 里，SDK 会忽略它而不是报错。

        这就是"响应体除 x_xingcha 外不加任何非 OpenAI 键"那条契约的理由——
        顶层加别的键，某些严格的客户端会拒绝解析。
        """
        upstream.reset()
        upstream.tool_payloads = [{"title": "t", "score": 1}]
        r = live.chat.completions.create(
            model="extract", messages=[{"role": "user", "content": "x"}]
        )
        # SDK 解析成功，且扩展块可以从原始响应里取到
        extra = r.model_extra or {}
        assert C.EXT_KEY in extra
        assert extra[C.EXT_KEY]["tier"] == Tier.T2.value


def test_data_dir_isolated(tmp_path: Path) -> None:
    assert tmp_path.exists()
