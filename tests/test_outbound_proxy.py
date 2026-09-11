"""所有出站客户端都要读环境代理，而且是同一个建法。

回归测试。此前有两处各建各的 ``httpx2.AsyncClient``：``upstream.make_client``
（拉模型目录、上游体检、直通）改成了 ``trust_env=True``，而 ``builder.make_provider``
——**Agent 真正调模型走的就是它**——还留着 ``False``。

症状极具迷惑性：模型目录拉得到（几百个模型全在）、上游体检也是通的，**只有 Agent
调用**被上游按出口 IP 挡回 ``This model is not available in your region.``，而 502 的
文案完全指向上游，一点看不出是自己没走代理。

所以这里查两件事：真的读到了代理（行为），以及没有人再绕开那个工厂（结构）。
"""

from __future__ import annotations

import ast
from pathlib import Path

import httpx2
import pytest

import xingcha
from xingcha.core import builder
from xingcha.core.upstream import UpstreamConfig, make_client, new_async_client

SRC = Path(xingcha.__file__).parent
CFG = UpstreamConfig(api_key="sk-test", base_url="https://openrouter.ai/api/v1")


#: 一个不可能和真机撞上的端口。用 7890 一类常见值的话，机器自己的系统代理就能让
#: 断言恒真——守卫还挂着绿灯，却什么也没验。
PROXY_PORT = 18732


def _proxied_ports(client: httpx2.AsyncClient) -> set[int]:
    """这个客户端实际挂上的代理端口。

    断言"某个具体的代理挂上了"，而不是"mounts 非空"——Windows 上 ``trust_env=True``
    还会读进系统（注册表）里配的代理，于是"非空"在有些机器上恒为真，这条守卫就白挂了。
    看的是结果而不是 ``trust_env`` 这个输入。
    """
    ports: set[int] = set()
    for transport in getattr(client, "_mounts", {}).values():
        url = getattr(getattr(transport, "_pool", None), "_proxy_url", None)
        if url is not None and url.port is not None:
            ports.add(url.port)
    return ports


def _reads_proxy(client: httpx2.AsyncClient) -> bool:
    return PROXY_PORT in _proxied_ports(client)


@pytest.fixture
def proxied(monkeypatch: pytest.MonkeyPatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{PROXY_PORT}")


async def test_passthrough_client_reads_env_proxy(proxied):
    client = make_client(CFG, timeout=5.0)
    try:
        assert _reads_proxy(client)
    finally:
        await client.aclose()


async def test_agent_provider_reads_env_proxy(proxied):
    """**这条是那个 bug 的靶心。** Agent 调模型走 make_provider。"""
    provider = builder.make_provider(CFG, timeout=5.0)
    http = provider.client._client  # type: ignore[attr-defined]
    assert _reads_proxy(http), (
        "make_provider 建的客户端没读环境代理——Agent 调用会绕过代理直连上游，"
        "而模型目录与上游体检照常可用，症状全指向上游。"
    )
    await http.aclose()


async def test_both_paths_agree(proxied):
    """两条路必须给出同一个答案。不等的那一刻就是这个 bug 本身。"""
    a = make_client(CFG, timeout=5.0)
    b = builder.make_provider(CFG, timeout=5.0).client._client  # type: ignore[attr-defined]
    try:
        assert _reads_proxy(a) == _reads_proxy(b)
    finally:
        await a.aclose()
        await b.aclose()


async def test_socks_without_socksio_falls_back_to_direct(monkeypatch: pytest.MonkeyPatch):
    """``ALL_PROXY=socks5://…`` 而没装 socksio 时，**退回直连而不是起不来**。

    httpx 在**构造阶段**就抛 ImportError。不兜住的话整个服务起不来，而报错
    （``socksio not installed``）完全看不出跟"机器上配了个代理"有关。一台配了 socks
    的机器本来是能直连上游的，不该因为读代理失败就整个不可用。

    直接造 ImportError 而不是真去卸 socksio：后者依赖装了什么，这条要在任何机器上
    都给出同一个答案。
    """
    real = httpx2.AsyncClient
    calls: list[bool] = []

    def fake(*args, **kwargs):
        calls.append(kwargs.get("trust_env", True))
        if kwargs.get("trust_env"):
            raise ImportError("Using SOCKS proxy, but the 'socksio' package is not installed.")
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx2, "AsyncClient", fake)
    client = new_async_client()
    try:
        assert calls == [True, False], "应当先试读代理，失败后退回 trust_env=False"
        assert isinstance(client, real)
    finally:
        await client.aclose()


def test_nobody_builds_a_raw_client():
    """``httpx2.AsyncClient(...)`` 只许出现在 ``new_async_client`` 里面。

    这条是结构守卫，拦的是下一次：再加一个出站客户端而忘了走工厂，行为测试只会
    盯着已知的那两条路，新的那条照样漏。socks 的 ImportError 兜底同理——散在几处
    就一定有一处会漏。
    """
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

        allowed: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "new_async_client":
                allowed = {ln for n in ast.walk(node) if (ln := getattr(n, "lineno", None))}

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if name == "AsyncClient" and node.lineno not in allowed:
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")

    assert not offenders, (
        f"这些地方直接建了 httpx2.AsyncClient：{offenders}。"
        f"改走 core.upstream.new_async_client——代理与 socks 兜底只有一处定义。"
    )
