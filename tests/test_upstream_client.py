"""上游客户端的构造。

这几条是"代理不进代码"这个承诺的直接证据。整套测试在 ALL_PROXY 指黑洞的环境下
也要通过，但那只证明"没用到代理"；下面额外断言"客户端确实关闭了 trust_env"，
因为前者在代码恰好不发请求时会假阳性。
"""

from __future__ import annotations

import httpx2
import pytest

from xingcha import contract as C
from xingcha.contract import Tier
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


# =============================================================================
# provider 的选择
# =============================================================================


class TestProviderChoice:
    """**不是 OpenRouter 就不能用 OpenRouterProvider。**

    它的 ``model_profile()`` 在模型名里没有 ``/`` 时直接抛 UserError。而厂商直连
    与大多数中转的模型 id 恰恰是裸的（``deepseek-v4-flash``），于是：

    * ``GET /v1/models`` 正常（那只是一次 HTTP 拉取），
    * 直通正常（原样转发），
    * **只有 Agent 挂**，而且是一句看不出原因的 500"服务内部错误"。

    也就是产品的核心卖点在任何非 OpenRouter 上游上都不可用。实测踩到过。
    """

    def test_openrouter_host_gets_the_openrouter_provider(self):
        from pydantic_ai.providers.openrouter import OpenRouterProvider

        from xingcha.core import builder
        from xingcha.core.upstream import UpstreamConfig

        p = builder.make_provider(
            UpstreamConfig(api_key="k", base_url="https://openrouter.ai/api/v1"), timeout=5
        )
        assert isinstance(p, OpenRouterProvider)

    def test_anything_else_gets_the_generic_one(self):
        from pydantic_ai.providers.openai import OpenAIProvider

        from xingcha.core import builder
        from xingcha.core.upstream import UpstreamConfig

        for base in (
            "https://api.deepseek.com/v1",
            "http://127.0.0.1:3000/v1",
            "https://my-relay.example.com/openrouter/v1",
        ):
            p = builder.make_provider(UpstreamConfig(api_key="k", base_url=base), timeout=5)
            assert isinstance(p, OpenAIProvider), base

    def test_a_bare_model_name_builds_against_a_direct_vendor(self):
        """这一条就是当初炸的那个场景。"""
        from xingcha.core import builder
        from xingcha.core.upstream import UpstreamConfig

        provider = builder.make_provider(
            UpstreamConfig(api_key="k", base_url="https://api.deepseek.com/v1"), timeout=5
        )
        rt = builder.build(
            spec_json={"model": "deepseek-v4-flash", "instructions": "hi"},
            tier=Tier.T3,
            out_schema=None,
            provider=provider,
            options=builder.BuildOptions(),
        )
        assert rt.model_id == "deepseek-v4-flash"

    def test_a_model_name_the_provider_rejects_is_not_an_internal_error(self):
        """构造失败要说清是什么失败了。

        ``make_model`` 放在 try 外面时，UserError 一路冒到最外层变成"服务内部错误，
        请把 run_id 给管理员"——而这类失败**每次都发生**，不是偶发，最需要说清原因。
        """
        from xingcha.core import builder
        from xingcha.core.upstream import UpstreamConfig
        from xingcha.errors import AgentBuildFailed

        provider = builder.make_provider(
            UpstreamConfig(api_key="k", base_url="https://openrouter.ai/api/v1"), timeout=5
        )
        with pytest.raises(AgentBuildFailed) as e:
            builder.build(
                spec_json={"model": "no-vendor-prefix", "instructions": "hi"},
                tier=Tier.T3,
                out_schema=None,
                provider=provider,
                options=builder.BuildOptions(),
            )
        assert "no-vendor-prefix" in str(e.value.log_detail or e.value)
