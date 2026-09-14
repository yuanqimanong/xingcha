"""导出再导入，不许丢东西。

回归测试。`agent export` 写的是纯 AgentSpec，而**分组与档位不是 spec 的字段**
（它们是 `agent.group_name` / `agent_version.tier` 两个列），于是导到另一台星槎上
会静默丢两样：

* **分组**掉回默认组 —— 还算显眼；
* **档位**退回自动判档，T1+/T3 一律变成 T2 —— 保证方式与花费都不同，而页面上
  看不出来，只有账单和失败形态会变。

修法是导出时把这两样盖进 `metadata.xingcha`（上游不解释 metadata，agent.yaml 仍是
标准 AgentSpec），导入时读完即删 —— 库里有列，spec 里再留一份就会不同步。

这里全是纯函数测试：不建库、不起服务、不碰网络。落库那一侧的优先级（显式 --group >
现有分组 > 导入物里的）由 test_agent_apply_group.py 与调用点的注释共同守。
"""

from __future__ import annotations

from xingcha.core.builder import SPEC_NS, stamp_origin, take_origin


def test_stamp_then_take_is_a_round_trip() -> None:
    stamped = stamp_origin({"model": "x-ai/grok-4.3"}, group="topic-data", tier="T3")
    assert stamped["metadata"][SPEC_NS] == {"group": "topic-data", "tier": "T3"}
    assert take_origin(stamped) == ("topic-data", "T3")


def test_stamp_merges_into_the_existing_namespace() -> None:
    """用户模板 / 少样本 / 输出通道住在同一个命名空间里，不能被覆盖掉。"""
    spec = {"metadata": {SPEC_NS: {"user_template": "**Input**:{{input}}"}}}
    stamped = stamp_origin(spec, group="g", tier="T2")
    assert stamped["metadata"][SPEC_NS] == {
        "user_template": "**Input**:{{input}}",
        "group": "g",
        "tier": "T2",
    }
    # 入参不能被改（导出那条路上同一份 spec 还有别的读者）
    assert spec["metadata"][SPEC_NS] == {"user_template": "**Input**:{{input}}"}


def test_take_removes_the_stamp_but_keeps_the_rest() -> None:
    """读完即删：库里分组/档位是列，spec 里留副本会立刻不同步。"""
    spec = stamp_origin({"metadata": {SPEC_NS: {"output_channel": "prompt"}}}, group="g", tier="T1")
    assert take_origin(spec) == ("g", "T1")
    assert spec["metadata"][SPEC_NS] == {"output_channel": "prompt"}


def test_take_cleans_up_an_emptied_namespace() -> None:
    """摘干净之后不留一坨空结构 —— 否则每个导入过的 spec 都多两层空字典。"""
    spec = stamp_origin({"model": "m"}, group="g", tier="T2")
    assert take_origin(spec) == ("g", "T2")
    assert "metadata" not in spec


def test_handwritten_yaml_without_a_stamp() -> None:
    """手写的 agent.yaml 没有来源戳，要给 (None, None) 让调用方退回原有行为。"""
    assert take_origin({"model": "m"}) == (None, None)
    assert take_origin({"model": "m", "metadata": {}}) == (None, None)
    assert take_origin({"model": "m", "metadata": {SPEC_NS: {}}}) == (None, None)
    # metadata.xingcha 被写成别的类型也不能炸
    assert take_origin({"metadata": {SPEC_NS: "nonsense"}}) == (None, None)


def test_blank_values_are_not_stamped() -> None:
    """默认组的 group 是 None，档位为空也一样 —— 别往文件里塞空键。"""
    assert stamp_origin({"model": "m"}, group=None, tier=None) == {"model": "m"}
    assert "metadata" not in stamp_origin({"model": "m"}, group="", tier="")


def test_exporter_stamps_the_origin() -> None:
    """守 exporter 里那次调用本身 —— 函数写对了但没接进导出，等于没修。"""
    import inspect

    from xingcha.core import exporter

    src = inspect.getsource(exporter.export)
    assert "stamp_origin(" in src and "group=group" in src
