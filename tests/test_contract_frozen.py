"""契约冻结物的黄金测试。

**这个文件红了不是测试坏了**，是在提醒你正在做一次破坏性变更。

调用方手里只有 ``base_url``、一把 ``sk-xc-`` key、一个 ``model`` 字符串。下面每条
断言都对应其中一环——改动任意一条，既有调用方在不改代码的前提下会开始收到不同的
结果，而且多半是静默的。要动就走 README §12 的契约号协商流程，别改这里的期望值。

期望值**写死在本文件里**，不从 ``contract`` 反向读取。从常量读取的"测试"只能证明
常量等于它自己，改一个闭集照样绿——那正是守卫漏查、却还挂着一盏绿灯的情形。
"""

from __future__ import annotations

import pytest

from xingcha import contract as C
from xingcha.contract import doc

# =============================================================================
# 闭集本身
# =============================================================================


def test_contract_version():
    assert C.CONTRACT_VERSION == 1


def test_features_only_grow():
    # 一个特性从缺失变存在是加法；反向（删一项、或某项从 True 变 False）是破坏性变更。
    assert set(C.FEATURES) == {
        "passthrough",
        "agents",
        "structured_output",
        "streaming_passthrough",
        "streaming_agents",
        "quota",
    }


def test_public_prefixes():
    # 顺序也冻结：app.py 按序挂载，换序会改变前缀重叠时的归属。
    assert C.PUBLIC_PREFIXES == ("/v1", "/api/v1", "/admin", "/healthz")


def test_own_v1_paths_is_closed():
    # 往这里加一项 = 从反代手里收回一条路径 = 调用方原本能用的上游端点突然变成
    # 星槎的语义。新增自有端点只能落在 /v1/xc/*。
    assert set(C.OWN_V1_PATHS) == {"models", "chat/completions"}
    assert C.RESERVED_V1_PREFIX == "xc"
    assert C.MODELS_ITEM_SEGMENTS == 1
    assert C.OPTIONS_ALWAYS_OWN is True


def test_token_envelope():
    assert C.TOKEN_PREFIX == "sk-xc-"
    assert C.TOKEN_KID_LEN == 16
    assert C.TOKEN_SCHEME_CURRENT == 1
    assert C.TOKEN_SCHEME_1_SECRET_LEN == 43
    assert C.TOKEN_SCHEME_1_ALG == "sha256"
    assert C.TOKEN_ENVELOPE_RE.pattern == (
        r"^sk-xc-(?P<scheme>[1-9][0-9]{0,2})-(?P<kid>[0-9a-z]{16})-(?P<secret>[A-Za-z0-9_-]{16,86})$"
    )


def test_token_schemes_never_shrink():
    # 删一个 scheme = 让那一批已签发的 key 集体失效，而"已签发 key 永不失效"是
    # 整个 kid-查表设计存在的理由。
    assert set(C.TOKEN_SCHEMES_SUPPORTED) >= {1}


def test_slug_charset_never_widens():
    # 放宽字符集会让原本 404 的字符串突然变成一个有效 Agent——行为的静默改变。
    assert C.SLUG_RE.pattern == r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$"
    assert (C.SLUG_MIN_LEN, C.SLUG_MAX_LEN) == (2, 48)
    assert set(C.SLUG_RESERVED) == {
        "models",
        "me",
        "health",
        "healthz",
        "readyz",
        "version",
        "xc",
        "admin",
        "api",
    }
    assert C.SLUG_RESERVED_PREFIX == "xc-"


def test_implicit_upstream_model_re_never_widens():
    # 隐式那条必须继续要求含 "/"：它是"不含 / 就是 Agent slug"的唯一判据，
    # 放宽等于让一个拼错的 slug 静默变成一次真实的付费调用。
    assert C.UPSTREAM_MODEL_RE.pattern == r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+(:[A-Za-z0-9._-]+)?$"
    assert C.EXPLICIT_NS == "xc:"
    assert set(C.EXPLICIT_KINDS) == {"agent", "model"}


def test_request_field_tristate():
    # 三个集合必须互斥：一个字段同时"认"和"拒"是规则本身自相矛盾，而实际行为
    # 取决于代码里先查哪个集合。
    assert set(C.REQUEST_HONOR) == {
        "model",
        "messages",
        "stream",
        "stream_options",
        "temperature",
        "top_p",
        "max_tokens",
        "max_completion_tokens",
        "stop",
        "seed",
        "presence_penalty",
        "frequency_penalty",
        "logit_bias",
    }
    assert set(C.REQUEST_IGNORE) == {"user", "store", "metadata", "n"}
    assert set(C.REQUEST_REJECT) == {
        "tools",
        "tool_choice",
        "functions",
        "function_call",
        "response_format",
        "retries",
        "max_retries",
        "usage_limits",
        "session_id",
    }
    assert not (set(C.REQUEST_HONOR) & set(C.REQUEST_IGNORE))
    assert not (set(C.REQUEST_HONOR) & set(C.REQUEST_REJECT))
    assert not (set(C.REQUEST_IGNORE) & set(C.REQUEST_REJECT))


def test_error_type_to_status():
    # 状态码进了调用方的重试逻辑：把 429 改成 503 会让"退避重试"变成"当作故障放弃"。
    assert {e.value: s for e, s in C.ERROR_HTTP_STATUS.items()} == {
        "invalid_api_key": 401,
        "quota_exceeded": 429,
        "model_not_found": 404,
        "model_invalid": 400,
        "param_unsupported": 400,
        "stream_unsupported": 400,
        "request_too_large": 413,
        "schema_violation": 422,
        "agent_spec_invalid": 400,
        "agent_build_failed": 500,
        "upstream_error": 502,
        "upstream_timeout": 504,
        "request_timeout": 504,
        "internal_error": 500,
    }


def test_every_error_type_has_a_status():
    # 漏一个就会在运行时 KeyError，而那条路径多半正是某个少见的失败分支。
    assert set(C.ERROR_HTTP_STATUS) == set(C.ErrorType)


def test_passthrough_defaults():
    # 直通加配额闸是**收紧**，必须经 quota_passthrough 能力位显式发布，
    # 不能靠改默认值。
    assert C.PASSTHROUGH_REQUIRES_AUTH is True
    assert C.PASSTHROUGH_ENFORCES_QUOTA is False
    assert C.FEATURE_QUOTA_PASSTHROUGH not in C.FEATURES


def test_response_shape():
    assert C.EXT_KEY == "x_xingcha"
    assert C.EXT_SHAPE_VERSION == 1
    # 成本用字符串：float 在 JSON 往返里会变成 0.00012300000000000001。
    assert C.COST_AS_STRING is True
    assert C.CONTENT_ALWAYS_STR is True
    assert C.SSE_DONE == "data: [DONE]\n\n"
    assert C.SSE_FRAME_ORDER == ("role", "content", "finish", "summary", "done")


def test_enum_values_frozen():
    # 这些字符串进了库、进了导出的 CSV、进了别人存下来的响应。
    assert [t.value for t in C.Tier] == ["T1", "T2", "T1P", "T3", "none"]
    assert [r.value for r in C.RunStatus] == [
        "ok",
        "schema_failed",
        "upstream_error",
        "quota",
        "timeout",
        "client_error",
    ]
    assert [c.value for c in C.CostSource] == [
        "openrouter_catalog",
        "genai_prices",
        "upstream",
        "unknown",
    ]


def test_runtime_invariants():
    # 单 worker 是配额、并发闸与 SQLite 单写者的共同前提，不是性能调优项。
    assert C.REQUIRED_WORKERS == 1
    assert C.CONTAINER_UID == 10001
    assert C.REQUIRED_JOURNAL_MODE == "wal"
    assert (C.DIR_MODE, C.FILE_MODE, C.UMASK) == (0o700, 0o600, 0o077)


# =============================================================================
# 分派规则：整个星槎唯一的路由决策点
# =============================================================================


@pytest.mark.parametrize(
    "raw, expect",
    [
        ("models", "models"),
        ("/models", "models"),
        ("models/", "models"),
        ("//models//", "models"),
        ("chat//completions", "chat/completions"),
        ("", ""),
    ],
)
def test_normalize_v1_path(raw, expect):
    assert C.normalize_v1_path(raw) == expect


def test_trailing_slash_stays_own():
    """``GET /v1/models/`` 必须仍判为自有路径。

    不归一化的话 FastAPI 的 redirect_slashes 在 catch-all 存在时不生效，请求直接被
    反代出去——客户端拿到 200、拿到几百个上游模型、一个 Agent 都看不到，且不报错。
    """
    for p in ("models", "/models", "models/", "/models/", "//models"):
        assert C.is_own_v1_path(p) is True


def test_case_sensitive_paths():
    # 折叠大小写会让 /v1/MODELS 也被自有路径接管，等于从反代手上多拿走一条路径。
    assert C.is_own_v1_path("Models") is False


def test_reserved_prefix_never_proxied():
    assert C.is_own_v1_path("xc/anything/at/all") is True


def test_unknown_paths_go_to_passthrough():
    for p in ("embeddings", "completions", "models/openai/gpt-4/endpoints"):
        assert C.is_own_v1_path(p) is False


@pytest.mark.parametrize(
    "model, kind, value, explicit",
    [
        # 1. xc: 显式命名空间
        ("xc:agent/extract", C.ModelKind.AGENT, "extract", True),
        ("xc:model/deepseek-v4", C.ModelKind.UPSTREAM, "deepseek-v4", True),
        ("xc:model/openai/gpt-5", C.ModelKind.UPSTREAM, "openai/gpt-5", True),
        # 2. 含 / → 上游裸模型
        ("openai/gpt-5", C.ModelKind.UPSTREAM, "openai/gpt-5", False),
        ("meta/llama-3:free", C.ModelKind.UPSTREAM, "meta/llama-3:free", False),
        # 3. 其余 → Agent slug
        ("extract", C.ModelKind.AGENT, "extract", False),
        ("my-agent-2", C.ModelKind.AGENT, "my-agent-2", False),
    ],
)
def test_classify_model(model, kind, value, explicit):
    ref = C.classify_model(model)
    assert (ref.kind, ref.value, ref.explicit) == (kind, value, explicit)


def test_explicit_channel_is_wider_than_implicit():
    """显式通道不要求含 ``/``，隐式通道要求。

    各家 id 命名不同：聚合方是 ``vendor/name``，直连厂商（DeepSeek / Groq / 智谱）
    没有斜杠。只认含斜杠的话，切到直连厂商后直通完全不可用。但隐式规则不能跟着放宽
    ——那条守着"拼错的 slug 不会静默变成一次真实的付费调用"。
    """
    assert C.classify_model("xc:model/deepseek-v4").kind is C.ModelKind.UPSTREAM
    # 同一个字符串不走显式通道时，是 Agent slug，不是上游模型
    assert C.classify_model("deepseek-v4").kind is C.ModelKind.AGENT


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "xc:",  # 缺 kind/value
        "xc:agent",  # 缺 /
        "xc:agent/",  # 空 value
        "xc:nope/x",  # 未知 kind
        "UPPER",  # 大写 slug
        "-leading",  # 连字符开头
        "trailing-",
        "has_underscore",
        "a",  # 太短
        "models",  # 保留字
        "xc-builtin",  # 保留前缀
        "vendor/",  # 隐式上游 id 缺后半段
    ],
)
def test_classify_model_rejects(bad):
    with pytest.raises(C.ModelRefInvalid):
        C.classify_model(bad)


def test_slug_too_long():
    with pytest.raises(C.ModelRefInvalid):
        C.validate_slug("a" * (C.SLUG_MAX_LEN + 1))


# =============================================================================
# 文档与常量同步
# =============================================================================


def test_readme_contract_section_is_generated():
    """README 的「对外契约」一节必须等于渲染结果。

    手写的契约文档一定会和代码漂移，而漂移之后你有两份互相矛盾的"权威"——更糟的是
    人会去信文档。改了常量就重新生成：``python -m xingcha.contract.doc``
    """
    readme = doc.DOC_PATH.read_text(encoding="utf-8")
    assert doc.extract(readme).strip() == doc.render().strip(), (
        "README 的「对外契约」一节与 contract 常量不同步。"
        "运行 `python -m xingcha.contract.doc` 重新生成。"
    )


def test_splice_leaves_the_rest_of_readme_untouched():
    # 生成器只许改标记之间那一段。碰到节外的字就意味着手写的部分会被机器悄悄改写。
    readme = doc.DOC_PATH.read_text(encoding="utf-8")
    spliced = doc.splice(readme)
    head, tail = readme.split(doc.MARK_BEGIN)[0], readme.split(doc.MARK_END)[1]
    assert spliced.startswith(head)
    assert spliced.endswith(tail)
