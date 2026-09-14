"""勾了「联网搜索」就必须真的联网。

回归测试。此前 ``WebSearch`` 能力被原样交给 pydantic-ai，它在 ``OpenAIChatModel`` 上
翻成 OpenAI 自家的 ``web_search_options``——而 **OpenRouter 不实现那个字段，静默丢弃**。
于是勾了等于没勾：不报错、不搜索，模型照常编一个自信的答案，成品上看不出来。

实测记录（2026-09-13，直连 OpenRouter）：

* ``web_search_options={"search_context_size":"banana"}`` → **200**，与一个瞎编的字段
  待遇相同；``reasoning_effort="banana"`` 与 ``plugins=[{"id":"banana"}]`` 都 400 并
  列出合法值——前者根本没被解析。
* grok-4.3 不带材料时答「BTC 约 $67K」，实价 $77,665。

所以这里守两件事：**plugins 被加上**，且 **WebSearch 这个 capability 被摘掉**
（不摘的话那个死字段照发）。纯函数测试，不建库、不起服务、不碰网络。
"""

from __future__ import annotations

from xingcha.core.builder import websearch_to_plugin


def _plugins(spec: dict) -> list:
    return spec.get("model_settings", {}).get("extra_body", {}).get("plugins", [])


def test_websearch_becomes_plugin_and_capability_is_dropped() -> None:
    out = websearch_to_plugin({"model": "x-ai/grok-4.3", "capabilities": ["WebSearch"]})
    assert _plugins(out) == [{"id": "web"}]
    # 摘不掉的话 pydantic-ai 仍会发 web_search_options —— 这条是这个 bug 的核心。
    assert out["capabilities"] == []


def test_other_capabilities_survive() -> None:
    out = websearch_to_plugin({"capabilities": ["Thinking", "WebSearch", "Instrumentation"]})
    assert out["capabilities"] == ["Thinking", "Instrumentation"]
    assert _plugins(out) == [{"id": "web"}]


def test_dict_shaped_capability_is_recognised() -> None:
    """``{"WebSearch": {...}}`` 与 ``{"name": "WebSearch"}`` 都是合法形状，不能漏。"""
    for shape in ({"WebSearch": {}}, {"name": "WebSearch"}):
        out = websearch_to_plugin({"capabilities": [shape]})
        assert out["capabilities"] == [], shape
        assert _plugins(out) == [{"id": "web"}], shape


def test_no_websearch_means_untouched() -> None:
    spec = {"model": "x-ai/grok-4.3", "capabilities": ["Thinking"]}
    assert websearch_to_plugin(spec) is spec
    assert websearch_to_plugin({"model": "x-ai/grok-4.3"}) == {"model": "x-ai/grok-4.3"}


def test_handwritten_plugin_wins() -> None:
    """手写的 web 插件（带 max_results / engine）优先，不被覆盖也不重复添加。"""
    mine = {"id": "web", "max_results": 15, "engine": "exa"}
    out = websearch_to_plugin(
        {
            "capabilities": ["WebSearch"],
            "model_settings": {"temperature": 0.1, "extra_body": {"plugins": [mine]}},
        }
    )
    assert _plugins(out) == [mine]
    assert out["model_settings"]["temperature"] == 0.1  # 别的模型参数不能被吃掉


def test_idempotent_and_does_not_mutate_input() -> None:
    spec = {"capabilities": ["WebSearch"], "model_settings": {"temperature": 0.1}}
    once = websearch_to_plugin(spec)
    assert websearch_to_plugin(once) == once
    # 入参必须原样：build() 之外还有导出/试运行等调用方共用同一份 spec。
    assert spec == {"capabilities": ["WebSearch"], "model_settings": {"temperature": 0.1}}


def test_build_applies_the_translation() -> None:
    """守 build() 里那次调用本身——函数写对了但没接进管线，等于没修。"""
    import inspect

    from xingcha.core import builder

    assert "websearch_to_plugin(spec)" in inspect.getsource(builder.build)
