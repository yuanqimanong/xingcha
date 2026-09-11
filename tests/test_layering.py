"""依赖方向。

------------------------------------------------------------------------------
架构标准 1 的守卫
------------------------------------------------------------------------------

架构标准 1（现记在 ARCHITECTURE.md 里）写着「单向依赖，无循环」，「怎么查」
那一栏写的是**「一个 import 方向检查脚本进 CI」**——那个脚本此前不存在，
所以这条标准一直只是一句形容词。

写成测试而不是独立脚本：脚本要有人记得运行，测试跟着全套一起跑。

------------------------------------------------------------------------------
为什么方向比"能不能跑"重要
------------------------------------------------------------------------------

反向 import 不会立刻坏事——Python 照样能跑。它的代价在**以后**：``core`` 一旦
依赖 ``services``，导出 bundle 那条「零星槎依赖」的卖点就开始漏；``contract``
一旦依赖任何东西，"契约是实现的约束"就倒挂成"契约跟着实现走"。

这类退化每次只退一小步，且每一步都有当时看来合理的理由。所以要机械地拦。
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "xingcha"

#: 层次从低到高。**低层不得 import 高层。**
#:
#: ``obs`` 与 ``db`` 同级：都是基础设施，被上面几层用，自己不回看业务。
#: 这一条是 v0.4 加 OTel 时才需要回答的问题——``services/run`` 与 ``core/builder``
#: 都 import 了 ``obs.tracing``，而原来的四层描述里没有 obs 的位置。
LAYERS = ["contract", "db", "obs", "core", "services", "api", "web"]

#: 不属于分层的顶层模块：它们是装配点或跨层工具，允许 import 任何层。
#:
#: ``app`` 与 ``cli`` 是装配点（把各层接起来正是它们的职责）；``config`` /
#: ``crypto`` / ``errors`` / ``bootstrap`` / ``contract_doc`` 是跨层基础件。
#:
#: **这个集合是白名单，由 :meth:`TestDependencyDirection.test_every_module_is_placed`
#: 强制执行。** 它此前只是一段注释——定义了却没有任何断言读它，于是新加一个顶层
#: 模块既不会落进某一层、也不会被要求登记在这里，方向检查就静悄悄地漏掉了它。
UNLAYERED = {
    "app",
    "cli",
    "config",
    "crypto",
    "errors",
    "bootstrap",
    "contract_doc",
    "pricing",
    "__init__",
}


def module_layer(path: Path) -> str | None:
    """这个文件属于哪一层。不属于分层的返回 None（它们必须登记在 :data:`UNLAYERED`）。"""
    rel = path.relative_to(SRC)
    head = rel.parts[0]
    if head in LAYERS:
        return head
    if head == "contract.py":
        return "contract"
    return None


def unlayered_name(path: Path) -> str:
    """不属于分层的那些模块的登记名：``app.py`` → ``app``，``cli/db.py`` → ``cli``。"""
    head = path.relative_to(SRC).parts[0]
    return head.removesuffix(".py")


def internal_imports(path: Path) -> set[str]:
    """这个文件 import 了本包的哪些层。

    同时认绝对（``from xingcha.core import x``）与相对（``from ..core import x``）
    两种写法——只查一种的话，换一种写法就能绕过守卫。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    rel = path.relative_to(SRC)
    depth = len(rel.parts) - 1  # 当前模块所在包相对 xingcha 的深度
    out: set[str] = set()

    def note(dotted: str) -> None:
        head = dotted.split(".")[0]
        if head in LAYERS:
            out.add(head)
        elif head == "contract":
            out.add("contract")

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module and node.module.startswith("xingcha."):
                    note(node.module[len("xingcha.") :])
                elif node.module == "xingcha":
                    for alias in node.names:
                        note(alias.name)
            else:
                # 相对 import：level=1 是当前包，level=2 是上一层……
                up = node.level - 1
                if up > depth:
                    continue
                if node.module:
                    note(node.module) if up == depth else None
                if up == depth:
                    # from ..core import x  → module 就是层名
                    if node.module:
                        note(node.module)
                    else:
                        for alias in node.names:
                            note(alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("xingcha."):
                    note(alias.name[len("xingcha.") :])
    return out


def all_sources() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if "migrations" not in p.parts)


class TestDependencyDirection:
    def test_the_probe_itself_sees_real_imports(self):
        """先证明探针有效。

        探针失效的话，下面所有断言都会"通过"——而那正是最坏的情况：
        一条绿灯说明"方向是对的"，实际上什么都没查。
        """
        got = internal_imports(SRC / "services" / "run.py")
        assert "core" in got, f"services/run.py 明显 import 了 core，探针却只看到 {got}"
        assert "contract" in got

    @pytest.mark.parametrize("path", all_sources(), ids=lambda p: str(p.relative_to(SRC)))
    def test_every_module_is_placed(self, path: Path):
        """每个文件要么在某一层里，要么显式登记为"不分层"。

        没有这一条的话 :data:`UNLAYERED` 就只是一段注释——而下面那条方向断言对
        "既不在 LAYERS 里、也没人登记"的模块是直接 return 的，**新加的顶层模块
        会静默豁免掉整个方向检查**。守卫漏查比没有守卫更糟：它还挂着一盏绿灯。
        """
        if module_layer(path) is not None:
            return
        name = unlayered_name(path)
        assert name in UNLAYERED, (
            f"{path.relative_to(SRC)} 既不属于 {LAYERS} 中的任何一层，"
            f"也没有登记在 UNLAYERED 里。它是装配点/跨层基础件就加进 UNLAYERED，"
            f"否则把它挪进对应的层。"
        )

    @pytest.mark.parametrize("path", all_sources(), ids=lambda p: str(p.relative_to(SRC)))
    def test_no_module_imports_a_higher_layer(self, path: Path):
        layer = module_layer(path)
        if layer is None:
            return  # 装配点与跨层基础件，见 UNLAYERED
        rank = LAYERS.index(layer)
        violations = {
            dep for dep in internal_imports(path) if LAYERS.index(dep) > rank and dep != layer
        }
        assert not violations, (
            f"{path.relative_to(SRC)}（{layer} 层）import 了更高层：{sorted(violations)}。"
            f"方向必须是 {' → '.join(LAYERS)}"
        )

    def test_contract_is_at_the_bottom(self):
        """契约不 import 任何东西。

        倒挂之后"契约是实现的约束"就变成"契约跟着实现走"，而那是不可逆的：
        一旦契约依赖实现，改实现就会改契约。
        """
        assert internal_imports(SRC / "contract.py") == set()

    def test_core_does_not_import_services_or_api(self):
        """标准 1 点名的那一条，单独再断一次。

        ``core`` 是导出 bundle 的来源（``core/exporter.py``），它一旦依赖
        ``services``，「导出物零星槎依赖」这个卖点就开始漏。
        """
        bad = defaultdict(set)
        for path in all_sources():
            if module_layer(path) != "core":
                continue
            for dep in internal_imports(path) & {"services", "api", "web"}:
                bad[str(path.relative_to(SRC))].add(dep)
        assert not bad, f"core 层出现了反向依赖：{dict(bad)}"

    def test_no_import_cycles_between_layers(self):
        """层与层之间不许成环。

        单条 import 的方向都对，仍可能出现 A 层的 x 依赖 B 层、B 层的 y 依赖 A 层
        这种跨文件的环。逐文件断言 + 严格的层序已经排除了它，这条是显式记录。
        """
        edges = defaultdict(set)
        for path in all_sources():
            layer = module_layer(path)
            if layer is None:
                continue
            for dep in internal_imports(path):
                if dep != layer:
                    edges[layer].add(dep)
        for layer, deps in edges.items():
            for dep in deps:
                assert layer not in edges.get(dep, set()), f"{layer} 与 {dep} 互相依赖"
