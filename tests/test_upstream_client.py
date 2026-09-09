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
        # 原因要出现在**给调用方看的那句话**里，不只是日志里。这类失败每次都发生，
        # 只说"内部错误"等于让人去猜一个日志里明写着的答案。
        assert "no-vendor-prefix" in e.value.message
        assert "prefixed with the upstream provider" in e.value.message


class TestNativeOkNeedsBothSources:
    """T1 的前提要问**两个人**，取交集。

    真正的闸在 pydantic-ai 里、在本地发请求之前就会拦：

        if output_mode == 'native' and not profile.get('supports_json_schema_output', False):
            raise UserError('Native structured output is not supported by this model.')

    而判档此前只问模型目录。两个来源各错一个方向（实测）：``z-ai/glm-5.3-flash``
    在 OpenRouter 目录里标着 structured_outputs: true，profile 说 false——于是判档
    保住 T1、保存时**不给任何降级提示**，然后每一次调用都失败。管理员以为拿到了
    最强的形状保证，实际拿到一个必然报错的 Agent。
    """

    def _provider(self, base: str):
        from xingcha.core import builder
        from xingcha.core.upstream import UpstreamConfig

        return builder.make_provider(UpstreamConfig(api_key="k", base_url=base), timeout=5)

    def test_catalog_yes_profile_no_is_no(self):
        from xingcha.core import builder

        p = self._provider("https://openrouter.ai/api/v1")
        assert builder.native_ok("z-ai/glm-5.3-flash", p, catalog_says=True) is False

    def test_catalog_no_short_circuits(self):
        """目录说不支持就不必再问——也不该因为通用 profile 的默认值把它翻成 yes。

        厂商直连时目录里常常连能力字段都没有（DeepSeek 的 /models 只回
        id/object/owned_by），而通用 profile 对没见过的名字给的是默认值。
        """
        from xingcha.core import builder

        p = self._provider("https://api.deepseek.com/v1")
        assert builder.native_ok("deepseek-v4-flash", p, catalog_says=False) is False

    def test_both_yes_is_yes(self):
        from xingcha.core import builder

        p = self._provider("https://openrouter.ai/api/v1")
        assert builder.native_ok("openai/gpt-5", p, catalog_says=True) is True

    def test_an_unbuildable_model_name_is_no_not_a_crash(self):
        """判档路径上抛异常的话，保存表单会 500——而它只是想知道该显示哪一档。"""
        from xingcha.core import builder

        p = self._provider("https://openrouter.ai/api/v1")
        assert builder.native_ok("no-vendor-prefix", p, catalog_says=True) is False
