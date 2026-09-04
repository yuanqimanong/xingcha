"""用户提交的 JSON Schema 的安全护栏。

准入项 A5（ReDoS）与 A6（校验期 SSRF）都在这里。两者的共同点是：**攻击载荷看起来
完全是一份正常的 schema**，管理员自己都不一定看得出问题。
"""

from __future__ import annotations

import json

import pytest

from xingcha import contract as C
from xingcha.core.schema_guard import SchemaRejected, make_validator, validate_schema

OK = {"type": "object", "properties": {"title": {"type": "string"}}}


class TestReDoS:
    """A5：正则由 Python 的 re 在事件循环上执行，且每次校验重试都重跑一遍。

    一条灾难性回溯的正则就能把一核打满，而整个星槎是单进程——服务直接停摆。
    """

    @pytest.mark.parametrize(
        "schema",
        [
            {"type": "object", "properties": {"a": {"type": "string", "pattern": "(a+)+$"}}},
            {"type": "object", "patternProperties": {"^x": {"type": "string"}}},
            # 藏在 items 里 —— 只走 properties 的实现会漏掉
            {
                "type": "object",
                "properties": {"a": {"type": "array", "items": {"pattern": "(a+)+"}}},
            },
            # 藏在 anyOf 里
            {"type": "object", "properties": {"a": {"anyOf": [{"pattern": "(a+)+"}]}}},
            # 藏在 $defs 里
            {
                "type": "object",
                "properties": {"a": {"$ref": "#/$defs/T"}},
                "$defs": {"T": {"type": "string", "pattern": "(a+)+"}},
            },
        ],
    )
    def test_pattern_is_rejected_wherever_it_hides(self, schema: dict):
        with pytest.raises(SchemaRejected) as e:
            validate_schema(schema)
        assert "pattern" in str(e.value)

    def test_error_suggests_an_alternative(self):
        """只说"不支持"会让人卡住。要告诉他们改用什么。"""
        with pytest.raises(SchemaRejected) as e:
            validate_schema({"type": "object", "properties": {"a": {"pattern": "x"}}})
        assert "enum" in str(e.value)


class TestSSRF:
    """A6：jsonschema 在没有封闭 registry 时会**真的去取**远程引用。"""

    @pytest.mark.parametrize(
        "ref",
        [
            "https://evil.example/x.json",
            "http://169.254.169.254/latest/meta-data/",
            "file:///etc/passwd",
            "//evil.example/x.json",
        ],
    )
    def test_non_local_refs_rejected(self, ref: str):
        with pytest.raises(SchemaRejected):
            validate_schema({"type": "object", "properties": {"a": {"$ref": ref}}})

    def test_remote_ref_would_otherwise_slip_through(self):
        """反证：上游的 StructuredDict **不会**拦住远程 $ref。

        没有这条，上面那些测试只是"我们加了个检查"；有了它才说明那个检查在挡什么。
        """
        from pydantic import TypeAdapter
        from pydantic_ai import StructuredDict

        sd = StructuredDict({"type": "object", "properties": {"a": {"$ref": "https://x/y.json"}}})
        assert "$ref" in json.dumps(TypeAdapter(sd).json_schema())

    def test_validator_cannot_fetch_remote_refs(self):
        """纵深防御的第二层：让远程取回在**结构上**不可能发生。

        光靠前缀检查是不够的——那是一道可能被绕过的检查。这里直接把一份带远程
        $ref 的 schema 塞进校验器：它必须**报错**，而不是去访问那个地址。
        """
        from referencing.exceptions import Unresolvable

        v = make_validator({"type": "object", "properties": {"a": {"$ref": "https://x/y.json"}}})
        with pytest.raises(Unresolvable):
            list(v.iter_errors({"a": 1}))

    def test_validator_works_normally(self):
        v = make_validator(OK)
        assert list(v.iter_errors({"title": "x"})) == []
        assert [e.message for e in v.iter_errors({"title": 1})]


class TestRefSupport:
    def test_non_recursive_defs_are_supported(self):
        """**不要整体拒绝 $ref。**

        文档原来的护栏一刀切拒掉，把合法 schema 挡在了门外——pydantic-ai 内部会
        把 $defs 内联展开，非递归引用是支持的。
        """
        out = validate_schema(
            {
                "type": "object",
                "properties": {"a": {"$ref": "#/$defs/T"}},
                "$defs": {"T": {"type": "string"}},
            }
        )
        assert out["properties"]["a"] == {"type": "string"}

    def test_returns_inlined_schema(self):
        """落库的必须是**展开后**的版本。

        落原文的话，校验器执行的约束与模型收到的约束不是同一份——一个通过、另一个
        不通过，而且没人看得出为什么。
        """
        out = validate_schema(
            {
                "type": "object",
                "properties": {"a": {"$ref": "#/$defs/T"}},
                "$defs": {"T": {"type": "string"}},
            }
        )
        assert "$ref" not in json.dumps(out)
        assert "$defs" not in json.dumps(out)

    def test_recursive_ref_rejected_with_explanation(self):
        with pytest.raises(SchemaRejected) as e:
            validate_schema(
                {
                    "type": "object",
                    "properties": {"k": {"$ref": "#/$defs/N"}},
                    "$defs": {"N": {"type": "object", "properties": {"k": {"$ref": "#/$defs/N"}}}},
                }
            )
        assert "递归" in str(e.value)

    def test_dangling_ref_rejected(self):
        """上游对这种情况抛的是**裸 KeyError**，不拦下就会变成运行期 500。"""
        with pytest.raises(SchemaRejected) as e:
            validate_schema({"type": "object", "properties": {"a": {"$ref": "#/$defs/NOPE"}}})
        assert "$defs" in str(e.value)


class TestShape:
    @pytest.mark.parametrize("schema", [{"type": "array", "items": {}}, {"type": "string"}, []])
    def test_non_object_root_rejected(self, schema):
        with pytest.raises(SchemaRejected):
            validate_schema(schema)

    def test_root_error_says_what_to_do(self):
        with pytest.raises(SchemaRejected) as e:
            validate_schema({"type": "array", "items": {"type": "string"}})
        assert "放进一个字段里" in str(e.value)

    def test_invalid_json_points_at_the_line(self):
        with pytest.raises(SchemaRejected) as e:
            validate_schema('{"type": "object", }')
        assert "第" in str(e.value)


class TestLimits:
    def test_too_deep(self):
        node: dict = {"type": "string"}
        for _ in range(C.SCHEMA_MAX_DEPTH + 3):
            node = {"type": "object", "properties": {"n": node}}
        with pytest.raises(SchemaRejected) as e:
            validate_schema(node)
        assert "深度" in str(e.value)

    def test_too_many_properties(self):
        schema = {
            "type": "object",
            "properties": {f"f{i}": {"type": "string"} for i in range(C.SCHEMA_MAX_PROPS + 5)},
        }
        with pytest.raises(SchemaRejected):
            validate_schema(schema)

    def test_enum_too_long(self):
        schema = {
            "type": "object",
            "properties": {"a": {"enum": [str(i) for i in range(C.SCHEMA_MAX_ENUM + 1)]}},
        }
        with pytest.raises(SchemaRejected):
            validate_schema(schema)

    def test_too_large(self):
        with pytest.raises(SchemaRejected):
            validate_schema(
                json.dumps({"type": "object", "description": "x" * (C.SCHEMA_MAX_BYTES + 10)})
            )


class TestHappyPath:
    def test_plain_object(self):
        assert validate_schema(OK)["type"] == "object"

    def test_accepts_json_string(self):
        assert validate_schema(json.dumps(OK))["type"] == "object"

    def test_realistic_extraction_schema(self):
        """一份真实的抽取 schema 必须能过——护栏不能严到没法用。"""
        out = validate_schema(
            {
                "type": "object",
                "properties": {
                    "客户名称": {"type": "string", "description": "合同甲方的完整名称"},
                    "金额": {"type": "number"},
                    "币种": {"type": "string", "enum": ["CNY", "USD", "EUR"]},
                    "条款": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"编号": {"type": "string"}, "内容": {"type": "string"}},
                            "required": ["编号", "内容"],
                        },
                    },
                },
                "required": ["客户名称", "金额"],
            }
        )
        assert "客户名称" in out["properties"]
