"""依赖方向：低层不许 import 高层。

反向 import 不会立刻坏事，代价在**以后**：``core`` 一旦依赖 ``services``，导出 bundle
那条「零星槎依赖」的卖点就开始漏；``contract`` 一旦依赖任何东西，"契约是实现的约束"
就倒挂成"契约跟着实现走"，而这一步不可逆。这类退化每次只退一小步，且每一步当时
都有合理的理由——所以要机械地拦。

逐文件解析 AST，绝对（``from xingcha.core import x``）与相对（``from ..core import x``）
两种写法都认。只看 import 语句本身，不做 import——测试不该因为装配顺序而红。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import xingcha

SRC = Path(xingcha.__file__).parent

#: 依赖顺序。左边的不许 import 右边的。
LAYERS: tuple[str, ...] = ("contract", "db", "obs", "core", "services", "api", "web")
RANK = {name: i for i, name in enumerate(LAYERS)}

#: 不属于任何一层，允许 import 任何层。
#:
#: - ``app`` / ``cli`` 是装配点：把各层接起来正是它们的职责
#: - ``config`` / ``bootstrap`` 是启动序列
#: - ``foundation`` 是密钥环与错误信封，谁都要用——塞进任何一层都会逼出反向依赖
UNLAYERED: frozenset[str] = frozenset(
    {"app", "cli", "config", "bootstrap", "foundation", "__init__"}
)


def _py_files() -> list[Path]:
    return [p for p in sorted(SRC.rglob("*.py")) if "__pycache__" not in p.parts]


def _is_module(name: str) -> bool:
    return (SRC / f"{name}.py").exists() or (SRC / name / "__init__.py").exists()


def _top_module(path: Path) -> str:
    """文件属于哪个顶层模块。``core/builder.py`` → ``core``；``app.py`` → ``app``。"""
    rel = path.relative_to(SRC)
    return rel.parts[0] if len(rel.parts) > 1 else rel.stem


def _imported_tops(path: Path) -> set[str]:
    """这个文件 import 了哪些 xingcha 顶层模块。"""
    tops: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("xingcha."):
                    tops.add(alias.name.split(".")[1])

        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # 相对 import：从本文件所在目录往上数 level-1 层，再接 module 路径。
                base = path.parent
                for _ in range(node.level - 1):
                    base = base.parent
                try:
                    parts = list(base.relative_to(SRC).parts)
                except ValueError:
                    continue  # 越过了包根，不是 xingcha 内部
                if node.module:
                    parts += node.module.split(".")
                if parts:
                    tops.add(parts[0])
                else:
                    # ``from .. import contract`` —— 顶层就在 names 里。但同一句式也
                    # 可能取的是包里的一个**名字**（``from ... import __version__``），
                    # 那不是依赖，所以只认磁盘上真有对应模块的。
                    tops.update(a.name for a in node.names if _is_module(a.name))
            elif node.module and node.module.startswith("xingcha"):
                parts = node.module.split(".")
                if len(parts) > 1:
                    tops.add(parts[1])

    return tops


@pytest.mark.parametrize("path", _py_files(), ids=lambda p: str(p.relative_to(SRC)))
def test_no_upward_import(path: Path):
    me = _top_module(path)
    if me in UNLAYERED:
        return

    for target in _imported_tops(path):
        if target == me or target in UNLAYERED:
            continue
        assert target in RANK, (
            f"{path.relative_to(SRC)} import 了未登记的顶层模块 {target!r}。"
            f"把它加进 LAYERS 或 UNLAYERED——漏查的守卫比没有守卫更糟，它还挂着一盏绿灯。"
        )
        assert RANK[target] <= RANK[me], (
            f"{path.relative_to(SRC)}（{me} 层）import 了更高层的 {target!r}。"
            f"依赖顺序：{' → '.join(LAYERS)}"
        )


def test_contract_imports_nothing_from_xingcha():
    """契约处在依赖图最底层，一个 xingcha 模块都不许 import。

    单独一条而不是靠上面的顺序断言：contract 的 rank 是 0，顺序断言只能拦住它
    import 更高层，拦不住它 import 同层——而 contract 就是一层。
    """
    for path in _py_files():
        if _top_module(path) != "contract":
            continue
        assert not _imported_tops(path) - {"contract"}, (
            f"{path.relative_to(SRC)} 依赖了别的模块。契约一旦依赖实现，"
            f"「契约是实现的约束」就倒挂成「契约跟着实现走」。"
        )


def test_web_is_imported_by_nobody():
    """``web`` 是叶子。被别人 import 意味着后台的东西漏进了对外路径。"""
    for path in _py_files():
        me = _top_module(path)
        if me in ("web", "app", "cli"):
            continue
        assert "web" not in _imported_tops(path), f"{path.relative_to(SRC)} import 了 web"


def test_every_module_is_placed():
    """每个顶层模块要么在某一层，要么在白名单里。

    新加一个顶层模块而不登记，会被这一条拦下——否则守卫会安静地跳过它。
    """
    known = set(LAYERS) | UNLAYERED
    found = {_top_module(p) for p in _py_files()}
    assert found <= known, f"未登记的顶层模块：{sorted(found - known)}"


def test_layer_list_matches_reality():
    # 反过来也查：LAYERS 里列了一个已经不存在的层，说明这份清单在腐烂。
    found = {_top_module(p) for p in _py_files()}
    assert set(LAYERS) <= found, f"LAYERS 里有不存在的层：{sorted(set(LAYERS) - found)}"
