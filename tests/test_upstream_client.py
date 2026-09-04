"""上游客户端的构造。

这几条是"代理不进代码"这个承诺的直接证据。整套测试在 ALL_PROXY 指黑洞的环境下
也要通过，但那只证明"没用到代理"；下面额外断言"客户端确实关闭了 trust_env"，
因为前者在代码恰好不发请求时会假阳性。
"""

from __future__ import annotations

import httpx2
import pytest

from xingcha import contract as C
from xingcha.core.upstream import (
    UpstreamConfig,
    UpstreamNotConfigured,
    UpstreamPool,
    attribution_headers,
    make_client,
)

CFG = UpstreamConfig(api_key="sk-or-v1-x", base_url="https://openrouter.ai/api/v1")


class TestProxyIsolation:
    def test_trust_env_is_off(self):
        """httpx2 默认 trust_env=True。

        实测：ALL_PROXY=socks5://... 时，未关闭 trust_env 的客户端在**构造阶段**就抛
        ImportError（socksio 未装）——服务起不来，且报错完全看不出跟代理有关。
        星槎的定位就是"代理不进代码"，继承机器代理与这个定位直接冲突。
        """
        client = make_client(CFG, timeout=10.0)
        # httpx 系不公开 trust_env，只有 _trust_env
        assert client._trust_env is False

    def test_constructs_under_socks_proxy(self, monkeypatch: pytest.MonkeyPatch):
        """这是 E1 的回归测试：带 socks5 代理时必须照常构造成功。"""
        monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:1")
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
        client = make_client(CFG, timeout=5.0)
        assert client._trust_env is False

    def test_default_really_would_break(self, monkeypatch: pytest.MonkeyPatch):
        """反证：不关 trust_env 的话确实会炸。

        没有这条，上面两条只是"我们设了个参数"；有了它才说明那个参数在挡什么。
        """
        monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:1")
        if "socksio" in _installed():
            pytest.skip("本机装了 socksio，构造不会失败")
        with pytest.raises(ImportError, match="socksio"):
            httpx2.AsyncClient(trust_env=True)


def _installed() -> set[str]:
    import importlib.util

    return {"socksio"} if importlib.util.find_spec("socksio") else set()


class TestConfig:
    def test_base_url_is_normalized(self):
        assert (
            UpstreamConfig(api_key="k", base_url="https://x/v1/").normalized_base()
            == "https://x/v1"
        )

    def test_attribution_headers_are_not_redundant(self):
        """传了 openai_client 后官方就**不再**注入 HTTP-Referer / X-Title。

        走中转时必须自建 client，所以这段手写的头是必需的，不是重复代码——
        别把它当冗余清理掉。
        """
        h = attribution_headers(UpstreamConfig(api_key="k", app_url="https://xc.example"))
        assert h["HTTP-Referer"] == "https://xc.example"
        assert h["X-Title"] == "Xingcha"

    def test_no_app_url_means_no_referer(self):
        assert "HTTP-Referer" not in attribution_headers(UpstreamConfig(api_key="k"))


class TestPool:
    async def test_unconfigured_error_is_actionable(self):
        """首次部署必然还没配 key，报错要说下一步做什么。"""
        pool = UpstreamPool(timeout=5.0)
        assert not pool.configured
        with pytest.raises(UpstreamNotConfigured) as e:
            pool.client()
        assert "xingcha config set" in str(e.value)

    async def test_same_config_does_not_churn_the_pool(self):
        """相同配置是空操作——否则每次读设置都会丢弃连接池。"""
        pool = UpstreamPool(timeout=5.0)
        await pool.set_config(CFG)
        first = pool.client()
        await pool.set_config(CFG)
        assert pool.client() is first
        await pool.aclose()

    async def test_changed_config_rebuilds(self):
        pool = UpstreamPool(timeout=5.0)
        await pool.set_config(CFG)
        first = pool.client()
        await pool.set_config(UpstreamConfig(api_key="sk-or-v1-y", base_url="https://relay/v1"))
        assert pool.client() is not first
        await pool.aclose()

    async def test_default_base_url_matches_contract(self):
        assert UpstreamConfig(api_key="k").base_url == C.OPENROUTER_DEFAULT_BASE_URL
