"""用户提交的 JSON Schema 的安全护栏。

管理员在表单里填的 schema 会被两处消费：发给模型当约束，以及在本地做运行时校验。
两条路径都能被一份恶意 schema 变成攻击面，所以这一层不是可选的。

**它同时负责一件容易被忽略的正确性工作：返回内联展开后的 schema。**
``StructuredDict`` 内部会把 ``$defs`` 内联掉再发给模型；如果本地校验器用的是带
``$ref`` 的原文，那么模型收到的约束与校验器执行的约束**不是同一份**——一个通过、
另一个不通过，而且没人看得出为什么。所以落库的必须是展开后的版本。

拦截的东西与理由（全部实测过）：

``pattern`` / ``patternProperties``
    jsonschema 用 Python 的 ``re`` 执行它们，**在事件循环上，且每次 schema 重试都重跑
    一遍**。一条 ``(a+)+$`` 就能把一核打满，而整个星槎是单进程——服务直接停摆。
    这是准入项 A5。

非 ``#/`` 开头的 ``$ref``
    jsonschema 在没有封闭 registry 时会**真的去取**远程引用。实测
    ``{"$ref": "https://evil.example/x.json"}`` 能静默穿过 ``StructuredDict``，
    于是校验期就成了一个 SSRF 原语。这是准入项 A6。除了这里拒绝，构造校验器时还要
    传入空的 registry，让远程取回在结构上不可能发生。

递归 ``$ref`` / 悬空 ``$ref`` / 非 object 根
    ``StructuredDict`` 对前两者分别抛 ``UserError`` 与裸 ``KeyError``，对第三者抛
    ``UserError``。不在保存时拦下，就会变成运行期的 500。

深度 / 字段数 / enum 长度 / 字节数
    资源耗尽的常规护栏。
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import TypeAdapter
from pydantic_ai import StructuredDict
from pydantic_ai.exceptions import UserError

from .. import contract as C


class SchemaRejected(ValueError):
    """schema 不被接受。

    消息直接展示给填表的人，所以必须说清楚**哪里**不行、**为什么**不行——
    只说"schema 无效"会让人反复试错。
    """


def _walk(node: Any, *, depth: int, props: list[int], path: str) -> None:
    """递归检查禁用关键字与规模上限。"""
    if depth > C.SCHEMA_MAX_DEPTH:
        raise SchemaRejected(f"嵌套深度超过 {C.SCHEMA_MAX_DEPTH} 层（在 {path or '根'}）")

    if isinstance(node, list):
        for i, item in enumerate(node):
            _walk(item, depth=depth, props=props, path=f"{path}[{i}]")
        return
    if not isinstance(node, dict):
        return

    for keyword in C.SCHEMA_FORBIDDEN_KEYWORDS:
        if keyword in node:
            raise SchemaRejected(
                f"不支持 `{keyword}`（在 {path or '根'}）。\n"
                "正则由 Python 的 re 在服务进程里执行，且每次校验重试都会重跑一遍；"
                "一条灾难性回溯的正则就能让整个服务停摆。\n"
                "请改用 enum、format、minLength/maxLength 表达约束。"
            )

    ref = node.get("$ref")
    if isinstance(ref, str) and not ref.startswith(C.SCHEMA_REF_ALLOWED_PREFIX):
        raise SchemaRejected(
            f"`$ref` 只能指向本文档内部（以 `{C.SCHEMA_REF_ALLOWED_PREFIX}` 开头），"
            f"收到 {ref!r}（在 {path or '根'}）。\n"
            "指向外部的引用会让校验过程去访问那个地址。"
        )

    enum = node.get("enum")
    if isinstance(enum, list) and len(enum) > C.SCHEMA_MAX_ENUM:
        raise SchemaRejected(
            f"enum 有 {len(enum)} 项，超过 {C.SCHEMA_MAX_ENUM}（在 {path or '根'}）"
        )

    properties = node.get("properties")
    if isinstance(properties, dict):
        for name, sub in properties.items():
            props[0] += 1
            if props[0] > C.SCHEMA_MAX_PROPS:
                raise SchemaRejected(f"字段总数超过 {C.SCHEMA_MAX_PROPS}")
            _walk(sub, depth=depth + 1, props=props, path=f"{path}/{name}" if path else name)

    # $defs / definitions 是「名字 → schema」的**映射**，不是 schema 本身。
    # 按普通子节点走的话只会检查那个映射对象（里面当然没有 pattern），
    # 而不会下降到每个定义里去——于是禁用关键字藏进 $defs 就能溜过去。
    # 这是测试抓出来的一个真实漏洞。
    for container in ("$defs", "definitions"):
        defs = node.get(container)
        if isinstance(defs, dict):
            for name, sub in defs.items():
                _walk(sub, depth=depth + 1, props=props, path=f"{path}/{container}/{name}")

    # 其余位置按普通子节点走。不走的话禁用关键字可以藏在 items 或 anyOf 里。
    for key in (
        "items",
        "prefixItems",
        "contains",
        "not",
        "if",
        "then",
        "else",
        "additionalProperties",
        "propertyNames",
        "allOf",
        "anyOf",
        "oneOf",
    ):
        sub = node.get(key)
        if isinstance(sub, dict | list):
            _walk(sub, depth=depth + 1, props=props, path=f"{path}/{key}" if path else key)


def validate_schema(raw: str | dict[str, Any]) -> dict[str, Any]:
    """校验并返回**内联展开后**的 schema。保存 Agent 时调用。

    返回值就是应当落库进 ``agent_version.out_schema`` 的东西——落原文会让校验器
    和模型看到两份不同的约束。
    """
    if isinstance(raw, str):
        if len(raw.encode("utf-8")) > C.SCHEMA_MAX_BYTES:
            raise SchemaRejected(f"schema 超过 {C.SCHEMA_MAX_BYTES // 1024} KB")
        try:
            schema = json.loads(raw)
        except json.JSONDecodeError as e:
            raise SchemaRejected(f"不是合法的 JSON：第 {e.lineno} 行 {e.msg}") from e
    else:
        schema = raw
        if len(json.dumps(schema).encode("utf-8")) > C.SCHEMA_MAX_BYTES:
            raise SchemaRejected(f"schema 超过 {C.SCHEMA_MAX_BYTES // 1024} KB")

    if not isinstance(schema, dict):
        raise SchemaRejected("schema 必须是一个 JSON 对象")
    if schema.get("type") != "object":
        raise SchemaRejected(
            f'顶层必须是 `"type": "object"`，收到 {schema.get("type")!r}。\n'
            "上游的结构化输出只接受对象作为根——要返回数组，把它放进一个字段里。"
        )

    _walk(schema, depth=0, props=[0], path="")

    # 交给 pydantic-ai 做最后一道，并**拿回它内联展开的结果**。
    # 这一步同时是探测式校验：递归 $ref 抛 UserError、悬空 $ref 抛裸 KeyError，
    # 都只有真的构造一次才会暴露。
    try:
        sd = StructuredDict(schema)
        inlined = TypeAdapter(sd).json_schema()
    except UserError as e:
        msg = str(e)
        if "recursive" in msg:
            raise SchemaRejected(
                "不支持递归的 `$ref`（字段直接或间接引用了自己）。\n"
                "上游的结构化输出无法表达无限嵌套；请把深度固定下来。"
            ) from e
        raise SchemaRejected(f"schema 不被接受：{msg}") from e
    except KeyError as e:
        raise SchemaRejected(
            f"`$ref` 指向了不存在的定义：{e}。检查 `$defs` 里有没有这个名字。"
        ) from e

    # 内联之后不该再有 $ref。真剩下了说明有我们没识别的引用形态，宁可拒绝。
    if "$ref" in json.dumps(inlined):
        raise SchemaRejected("展开后仍有无法解析的 `$ref`，请改成不含引用的写法。")

    return inlined


def make_validator(inlined: dict[str, Any]):
    """构造运行时校验器。

    **必须传入空的 registry。** 光靠 :func:`validate_schema` 拒绝远程 ``$ref`` 是
    不够的——那是一道可能被绕过的检查；空 registry 让远程取回在**结构上**不可能发生，
    是纵深防御里更靠底的那一层。
    """
    import jsonschema
    from referencing import Registry

    return jsonschema.Draft202012Validator(
        inlined,
        registry=Registry(),  # type: ignore[call-arg]
    )
