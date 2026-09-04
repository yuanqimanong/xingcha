"""Schema 字段命名建议。

依据：在 prompt、模型、输出结构、解码设置**全部固定**的前提下，仅改变 schema 字段的
措辞就能显著影响准确率——schema key 本身是一条隐式的指令通道。

这件事对星槎近乎量身定制：用户正在表单里敲字段名，此刻给建议的成本最低。实现是
一组规则，不调模型，不花钱。

**建议不阻断保存。** 这是提示不是规则——判断权在用户手里，我们只负责让他知道
有这么回事。一个把"建议"做成"拦截"的 lint 会很快被绕过或关掉。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from .guarantee import t1_rewrites_schema

Level = Literal["warn", "info"]


@dataclass(frozen=True, slots=True)
class Hint:
    path: str
    level: Level
    message: str


#: 过短或纯缩写：``id``、``t``、``v2``、``amt``
_TOO_SHORT = re.compile(r"^[a-z]{1,3}\d*$", re.IGNORECASE)

#: 语义空泛的名字。模型无法从它们推断出该填什么。
_GENERIC = frozenset(
    {
        "data",
        "value",
        "val",
        "result",
        "output",
        "item",
        "items",
        "obj",
        "object",
        "info",
        "content",
        "field",
        "fields",
        "text",
        "str",
        "num",
        "temp",
        "misc",
        "other",
        "extra",
        "meta",
        "payload",
        "body",
        "res",
        "resp",
    }
)

#: 匈牙利式前缀：``strName``、``intAge``。类型已经在 schema 里声明过了。
_HUNGARIAN = re.compile(r"^(str|int|bool|num|float|arr|obj|lst|dict)[A-Z_]")


def lint(schema: dict[str, Any], *, tier_is_native: bool = False) -> list[Hint]:
    """给出改进建议。空列表表示没什么可说的。

    ``tier_is_native`` 为真时额外提示 T1 档会把可选字段提升为必填——那是一件用户
    在表单上看不出来、但会真实改变模型行为的事。
    """
    hints: list[Hint] = []
    _walk(schema, "", hints)

    if tier_is_native:
        promoted = t1_rewrites_schema(schema)
        if promoted:
            hints.append(
                Hint(
                    path=", ".join(promoted),
                    level="warn",
                    message=(
                        "原生约束档下，上游会把这些可选字段**提升为必填**，模型必须输出它们。"
                        "如果这些字段确实可能没有值，改用校验重试档，或者把它们标成必填并"
                        "允许空值。"
                    ),
                )
            )

    if not schema.get("properties"):
        hints.append(Hint("", "warn", "schema 没有任何字段——模型不知道该输出什么。"))

    return hints


def _walk(node: dict[str, Any], path: str, hints: list[Hint]) -> None:
    props = node.get("properties")
    if not isinstance(props, dict):
        return

    for key, sub in props.items():
        here = f"{path}/{key}" if path else key
        if not isinstance(sub, dict):
            continue

        if _TOO_SHORT.match(key):
            hints.append(
                Hint(
                    here,
                    "warn",
                    f"「{key}」太短，模型难以推断它该装什么。"
                    "字段名本身就是一条指令——用完整的语义词，比如 invoice_number 而不是 no。",
                )
            )
        elif key.lower() in _GENERIC:
            hints.append(
                Hint(
                    here,
                    "warn",
                    f"「{key}」语义空泛。换成描述实际内容的名字，模型的准确率会有可观提升。",
                )
            )
        elif _HUNGARIAN.match(key):
            hints.append(
                Hint(here, "info", f"「{key}」带了类型前缀，而类型已经在 schema 里声明过了。")
            )

        if not sub.get("description"):
            hints.append(
                Hint(
                    here,
                    "info",
                    f"「{key}」没有 description。字段描述是一条有效的指令通道，"
                    "写一句「这个字段该填什么」通常比改提示词更直接。",
                )
            )

        if sub.get("type") == "object":
            _walk(sub, here, hints)
        elif sub.get("type") == "array" and isinstance(sub.get("items"), dict):
            _walk(sub["items"], f"{here}[]", hints)


def summarize(hints: list[Hint]) -> str:
    """一行摘要，给表单顶部用。"""
    warns = sum(1 for h in hints if h.level == "warn")
    infos = len(hints) - warns
    if not hints:
        return "字段命名看起来没问题。"
    parts = []
    if warns:
        parts.append(f"{warns} 条建议")
    if infos:
        parts.append(f"{infos} 条提示")
    return "、".join(parts) + "（不影响保存）"
