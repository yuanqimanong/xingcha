"""纯文本 Agent 的真流式。

------------------------------------------------------------------------------
这一层要守住的东西
------------------------------------------------------------------------------

从 v0.2 起，纯文本 Agent 对 ``stream=true`` 就返回 200 与一个合法的 SSE 帧序列，
只是内容只有一帧（伪流式）。当时那么做的理由写在契约里：若返回 400，客户端会为
那个 400 **写死绕过逻辑**，等真流式上线时反而被打断。

现在兑现那个承诺。所以这个文件里最重要的一条不是"能流"，而是**帧形状逐字未变**：
``id``/``created`` 全帧一致、role 帧在最前、``finish_reason`` 在内容之后、汇总帧带
usage 与扩展块、``data: [DONE]`` 收尾。帧数变多是兼容的，别的都不是。

第二重要的是**失败的表达方式**：
- 上游开流就失败 → 状态码还没提交，必须是正常的 JSON 错误，而不是"200 + 空流"；
- 流到一半失败 → 200 已经发了，只能靠**不发 [DONE]** 表达，同时照样记账。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator
from decimal import Decimal

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


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed(settings: Settings, base_url: str) -> str:
    settings.ensure_data_dir()
    migrate.upgrade_to_head(settings.db_path, settings.backup_dir)
    keyring = Keyring.load_or_create(settings.secret_path)

    async def go() -> str:
        engine = make_engine(settings.db_path)
        maker = make_sessionmaker(engine)
        async with maker() as s:
            await setting_svc.set_(s, keyring, C.SETTING_KEY_OPENROUTER_API_KEY, "sk-or-v1-fake")
            await setting_svc.set_(s, keyring, C.SETTING_KEY_OPENROUTER_BASE_URL, base_url)
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
            await agent_svc.save(
                s,
                slug="extract",
                name="抽取",
                description=None,
                instructions="抽取。",
                model="openai/gpt-5",
                schema_text=json.dumps(
                    {"type": "object", "properties": {"title": {"type": "string"}}}
                ),
                requested_tier=Tier.T2,
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
def live(tmp_path, upstream: FakeUpstream) -> Iterator[tuple[TestClient, str, Settings]]:
    """debounce 关掉：一片一帧，帧数才是可断言的事实而不是时序赌博。"""
    settings = Settings(
        data_dir=tmp_path / "data",
        request_timeout=10.0,
        catalog_ttl_seconds=60,
        stream_debounce_seconds=None,
    )
    token = _seed(settings, upstream.base_url)
    upstream.reset()
    with TestClient(create_app(settings)) as client:
        yield client, token, settings


def collect(client: TestClient, token: str, **overrides) -> tuple[int, str]:
    payload = {
        "model": "chat",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
        **overrides,
    }
    with client.stream("POST", "/v1/chat/completions", json=payload, headers=auth(token)) as r:
        return r.status_code, b"".join(r.iter_bytes()).decode()


def payloads(raw: str) -> list[dict]:
    frames = [f for f in raw.split("\n\n") if f.strip()]
    return [json.loads(f[6:]) for f in frames if f.strip() != C.SSE_DONE.strip()]


# =============================================================================
# 帧形状（契约冻结）
# =============================================================================


class TestFrameShape:
    def test_one_frame_per_upstream_chunk(self, live, upstream: FakeUpstream):
        """上游吐几片，客户端就收几个内容帧。

        这是"真流式"与"伪流式"唯一的可观测差别。合并成一帧的话，客户端那边就没有
        逐字显示可言——而那正是用户开 stream 的全部理由。
        """
        client, token, _ = live
        upstream.stream_chunks = ["永", "远", "不", "要", "相", "信", "上", "游"]
        _, raw = collect(client, token)

        content = [
            p["choices"][0]["delta"]["content"]
            for p in payloads(raw)
            if p["choices"] and "content" in p["choices"][0]["delta"]
        ]
        assert content == upstream.stream_chunks
        assert "".join(content) == "永远不要相信上游"

    def test_sequence_matches_the_frozen_contract(self, live, upstream: FakeUpstream):
        client, token, _ = live
        upstream.stream_chunks = ["a", "b"]
        _, raw = collect(client, token)

        assert raw.endswith(C.SSE_DONE)
        ps = payloads(raw)

        assert ps[0]["choices"][0]["delta"] == {"role": "assistant"}
        assert ps[1]["choices"][0]["delta"] == {"content": "a"}
        assert ps[2]["choices"][0]["delta"] == {"content": "b"}
        assert ps[3]["choices"][0] == {"index": 0, "delta": {}, "finish_reason": "stop"}
        assert ps[4]["choices"] == []
        assert ps[4]["usage"]["total_tokens"] >= 0
        assert ps[4][C.EXT_KEY]["v"] == C.EXT_SHAPE_VERSION
        assert all(p["object"] == "chat.completion.chunk" for p in ps)

    def test_id_and_created_are_stable_across_frames(self, live, upstream: FakeUpstream):
        """同一次响应的所有帧必须同 ``id``。

        每帧现算的话客户端没法把帧归到同一条回复上——SDK 会按 id 聚合。
        """
        client, token, _ = live
        upstream.stream_chunks = ["a", "b", "c"]
        _, raw = collect(client, token)
        ps = payloads(raw)
        assert len({p["id"] for p in ps}) == 1
        assert len({p["created"] for p in ps}) == 1
        assert ps[0]["id"].startswith("chatcmpl-")

    def test_empty_chunks_do_not_produce_frames(self, live, upstream: FakeUpstream):
        """上游的空心跳片不该变成客户端要解析的空帧。"""
        client, token, _ = live
        upstream.stream_chunks = ["a", "", "", "b"]
        _, raw = collect(client, token)
        content = [
            p["choices"][0]["delta"]["content"]
            for p in payloads(raw)
            if p["choices"] and "content" in p["choices"][0]["delta"]
        ]
        assert content == ["a", "b"]

    def test_no_content_length_header(self, live):
        """带 ``Content-Length`` 的流式响应等于空响应。

        真实 HTTP 客户端按它决定读多少字节，读到 0 就停——而且不报错，是最难查的
        那种失败。（``Response.__init__`` 会顺手加上这个头，所以 api/sse.py 故意
        不调它。）
        """
        client, token, _ = live
        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={"model": "chat", "messages": [{"role": "user", "content": "x"}], "stream": True},
            headers=auth(token),
        ) as r:
            assert "content-length" not in {k.lower() for k in r.headers}
            assert r.headers["content-type"].startswith("text/event-stream")
            b"".join(r.iter_bytes())


# =============================================================================
# 合帧
# =============================================================================


def test_debounce_merges_frames(tmp_path, upstream: FakeUpstream):
    """默认的 debounce 会合帧——这是有意的性能取舍，不是 bug。

    不合并的话一次长回答产生上千帧，每帧一次 JSON 序列化加一次 send，而人眼分不出
    0.05s。这条断言把"默认行为是合并"钉住，免得日后有人把默认值改成 None 却以为
    只是个无害的调参。
    """
    settings = Settings(data_dir=tmp_path / "data", request_timeout=10.0, catalog_ttl_seconds=60)
    assert settings.stream_debounce_seconds == 0.05
    token = _seed(settings, upstream.base_url)
    upstream.reset()
    upstream.stream_chunks = list("abcdefgh")
    upstream.stream_gap = 0.001  # 全落在同一个窗口里

    with TestClient(create_app(settings)) as client:
        _, raw = collect(client, token)

    content = [
        p["choices"][0]["delta"]["content"]
        for p in payloads(raw)
        if p["choices"] and "content" in p["choices"][0]["delta"]
    ]
    assert len(content) < 8, "同一窗口内的片应当被合并"
    assert "".join(content) == "abcdefgh", "合并不能丢内容"


# =============================================================================
# 失败的表达方式
# =============================================================================


class TestFailureIsHonest:
    def test_upstream_open_failure_is_a_json_error_not_an_empty_stream(
        self, tmp_path, upstream: FakeUpstream
    ):
        """开流就失败时状态码还没提交，必须给正常的错误响应。

        退化成"200 + 空流"的话，OpenAI SDK 的调用方拿到一个什么都不吐的迭代器：
        没有异常，没有内容，没有错误码——最难排查的一种失败。
        """
        settings = Settings(data_dir=tmp_path / "data", request_timeout=5.0, catalog_ttl_seconds=60)
        # 先用活着的上游 seed（要拿模型目录），再把 base_url 指到黑洞
        token = _seed(settings, upstream.base_url)
        keyring = Keyring.load_or_create(settings.secret_path)

        async def repoint() -> None:
            engine = make_engine(settings.db_path)
            maker = make_sessionmaker(engine)
            async with maker() as s:
                await setting_svc.set_(
                    s, keyring, C.SETTING_KEY_OPENROUTER_BASE_URL, "http://127.0.0.1:9/v1"
                )
                await s.commit()
            await engine.dispose()

        asyncio.run(repoint())

        with TestClient(create_app(settings)) as client:
            r = client.post(
                "/v1/chat/completions",
                json={
                    "model": "chat",
                    "messages": [{"role": "user", "content": "x"}],
                    "stream": True,
                },
                headers=auth(token),
            )
        assert r.status_code == 502
        assert r.json()["error"]["type"] == C.ErrorType.UPSTREAM_ERROR.value
        assert "text/event-stream" not in r.headers.get("content-type", "")

    def test_mid_stream_failure_omits_done_but_keeps_the_bill(self, live, upstream: FakeUpstream):
        """流到一半上游挂了：已发的帧保留、**不发 [DONE]**、账照样记。

        200 已经发出去，状态码改不了——所以"没有 [DONE]"就是唯一能用的失败信号
        （OpenAI 自己也是这个行为）。同时那半截流是真花了钱的，不记就等于给失败
        打折，而失败恰好是最贵的一类。
        """
        import sqlite3

        client, token, settings = live
        upstream.stream_chunks = list("abcdef")
        upstream.stream_abort_after = 3

        status, raw = collect(client, token)
        assert status == 200
        assert C.SSE_DONE not in raw, "中途失败**不能**发 [DONE]，否则客户端以为成功"

        content = [
            p["choices"][0]["delta"]["content"]
            for p in payloads(raw)
            if p["choices"] and "content" in p["choices"][0]["delta"]
        ]
        assert content == ["a", "b", "c"], "断流之前收到的帧应当保留"
        assert not any(p["choices"] == [] for p in payloads(raw)), "没有汇总帧"

        client.__exit__(None, None, None)
        with sqlite3.connect(settings.db_path) as c:
            c.row_factory = sqlite3.Row
            row = c.execute(
                "SELECT r.status, r.error_type FROM run r ORDER BY r.started_at"
            ).fetchall()[-1]
        assert row["status"] == C.RunStatus.UPSTREAM_ERROR.value
        assert row["error_type"] == C.ErrorType.UPSTREAM_ERROR.value

    def test_silent_truncation_is_not_reported_as_success(self, live, upstream: FakeUpstream):
        """**上游流被截断时不抛任何异常**——这是这一层最危险的一件事。

        实测：连接断了，``stream_text`` 的迭代静默结束，``is_complete`` 照样是 True。
        唯一的判据是最后那条 ModelResponse 有没有 finish_reason。

        判成功的话，一次被砍掉一半的回答会带着 ``finish_reason: "stop"`` 和
        ``[DONE]`` 交到客户端手上——它连察觉的机会都没有。静默的数据损坏比一个
        可检测的失败信号糟得多，所以这里必须判失败。
        """
        client, token, _ = live
        upstream.stream_chunks = list("abcdef")
        upstream.stream_finish_reason = None  # 中转不发结束原因

        status, raw = collect(client, token)
        assert status == 200
        assert C.SSE_DONE not in raw, "确认不了完整就不能发 [DONE]"
        assert not any(
            "finish_reason" in (p["choices"][0] if p["choices"] else {}) for p in payloads(raw)
        ), "更不能发 finish_reason: stop"

    def test_length_finish_reason_is_passed_through(self, live, upstream: FakeUpstream):
        """``length`` 必须原样传给客户端。

        它是"答案被 max_tokens 砍了"的唯一信号；改写成 ``stop`` 就等于告诉客户端
        这是一个完整的答案。
        """
        client, token, _ = live
        upstream.stream_finish_reason = "length"
        _, raw = collect(client, token)
        assert raw.endswith(C.SSE_DONE)
        finishes = [
            p["choices"][0]["finish_reason"]
            for p in payloads(raw)
            if p["choices"] and p["choices"][0].get("finish_reason")
        ]
        assert finishes == ["length"]

    def test_structured_agent_still_refuses_streaming(self, live):
        """结构化 Agent 对 ``stream=true`` 仍然是 400。

        真流式上线**不能**顺手把它也放开：流一半的 JSON 客户端解析不了，而它拿到
        的会是"格式错误"而不是"不支持流式"，排查方向完全反了。
        """
        client, token, _ = live
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


# =============================================================================
# 计量
# =============================================================================


class TestStreamingIsMetered:
    def _row(self, settings: Settings):
        import sqlite3

        with sqlite3.connect(settings.db_path) as c:
            c.row_factory = sqlite3.Row
            return c.execute(
                "SELECT r.kind, r.model, r.status, u.input_tokens, u.output_tokens, "
                "u.cost_usd, u.cost_source "
                "FROM run r JOIN run_usage u ON u.run_id = r.id ORDER BY r.started_at"
            ).fetchall()[-1]

    def test_run_is_recorded_with_usage(self, live, upstream: FakeUpstream):
        """流式的用量只有流结束后才知道——所以落库必须发生在收尾，不能在开头。

        在开头记的话每一条流式 run 的 token 都是 0，而流式恰好是长回答的主要形态。
        """
        client, token, settings = live
        upstream.stream_chunks = ["a", "b", "c"]
        collect(client, token)
        client.__exit__(None, None, None)

        row = self._row(settings)
        assert (row["kind"], row["model"], row["status"]) == ("agent", "chat", "ok")
        assert row["input_tokens"] == 10
        assert row["output_tokens"] == 5
        assert Decimal(row["cost_usd"]) > 0

    def test_summary_frame_carries_the_same_run_id(self, live, upstream: FakeUpstream):
        """汇总帧里的 ``run_id`` 必须是落库那一行的 id。

        对不上的话"客户端报了个 run_id，去日志里查"这条排查路径直接断掉。
        """
        client, token, settings = live
        _, raw = collect(client, token)
        run_id = payloads(raw)[-1][C.EXT_KEY]["run_id"]
        client.__exit__(None, None, None)

        import sqlite3

        with sqlite3.connect(settings.db_path) as c:
            ids = [r[0] for r in c.execute("SELECT id FROM run").fetchall()]
        assert run_id in ids


# =============================================================================
# 并发与断开
# =============================================================================


def test_concurrent_streams_do_not_break_the_concurrency_gate(tmp_path, upstream: FakeUpstream):
    """并发流式不能把并发闸弄坏。

    pydantic-ai 的并发闸是 anyio 的 ``CapacityLimiter``，它按 ``current_task()``
    记账——acquire 与 release **必须在同一个任务里**。starlette 的 StreamingResponse
    会在子任务里迭代响应体，于是"先 anext 探错、剩下交给它"这种写法会让 acquire 落在
    请求任务、release 落在子任务，直接

        RuntimeError: this borrower isn't holding any of this CapacityLimiter's tokens

    单条请求跑通不能证明这件事对（第一次 acquire 总会成功）；要并发跑过闸才算。
    """
    import threading

    settings = Settings(
        data_dir=tmp_path / "data",
        request_timeout=10.0,
        catalog_ttl_seconds=60,
        stream_debounce_seconds=None,
    )
    token = _seed(settings, upstream.base_url)
    upstream.reset()
    upstream.stream_chunks = ["a", "b", "c"]
    upstream.stream_gap = 0.02

    results: list[str] = []
    errors: list[BaseException] = []

    with TestClient(create_app(settings)) as client:

        def one() -> None:
            try:
                _, raw = collect(client, token)
                results.append(raw)
            except BaseException as e:
                errors.append(e)

        threads = [threading.Thread(target=one) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

    assert not errors, f"并发流式炸了：{errors[:2]}"
    assert len(results) == 8
    assert all(r.endswith(C.SSE_DONE) for r in results), "每一条都要正常收尾"


def test_client_disconnect_still_records_the_run(tmp_path, upstream: FakeUpstream):
    """客户端读一半就走，这次调用照样要落账。

    这条路径既不走正常收尾也不走异常收尾：生成器被 ``aclose()``，``yield`` 处抛
    GeneratorExit。不在 ``finally`` 里结算的话，上游的钱花了而账上一片空白——而
    "客户端中途关掉"在聊天客户端里是**常态**，不是边缘情况。

    **必须用真服务器 + 真 socket。** TestClient 会把整个响应先跑完再交给
    ``iter_bytes()``，此时生成器早已正常收尾——那样写出来的测试摘掉 ``finally``
    也是绿的，等于没测。
    """
    import socket
    import sqlite3
    import threading

    import uvicorn

    settings = Settings(
        data_dir=tmp_path / "data",
        request_timeout=10.0,
        catalog_ttl_seconds=60,
        stream_debounce_seconds=None,
    )
    token = _seed(settings, upstream.base_url)
    upstream.reset()
    upstream.stream_chunks = list("abcdefghijklmnop")
    upstream.stream_gap = 0.05  # 总共 ~0.8s，够我们在中途撒手

    port = 8896
    server = uvicorn.Server(
        uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="error")
    )
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "星槎没能启动"

    try:
        body = json.dumps(
            {"model": "chat", "messages": [{"role": "user", "content": "x"}], "stream": True}
        )
        request = (
            "POST /v1/chat/completions HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"Authorization: Bearer {token}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            "\r\n"
            f"{body}"
        )
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        sock.sendall(request.encode())
        first = sock.recv(4096)
        assert b"200 OK" in first, first[:200]
        # 硬关：不等流跑完，也不发 FIN 之后再读
        sock.close()

        # 给服务端时间走完 finally 与 flush
        deadline = time.monotonic() + 10
        rows: list = []
        while time.monotonic() < deadline:
            with sqlite3.connect(settings.db_path) as c:
                c.row_factory = sqlite3.Row
                rows = c.execute(
                    "SELECT r.status, u.input_tokens FROM run r JOIN run_usage u ON u.run_id = r.id"
                ).fetchall()
            if rows:
                break
            time.sleep(0.2)
    finally:
        server.should_exit = True

    assert rows, "断开的流式调用没有留下任何 run 行——上游的钱花了，账上一片空白"
