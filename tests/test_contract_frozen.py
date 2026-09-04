"""契约的黄金测试。

**这些测试变红不代表测试坏了，代表你正在做一次破坏性变更。**

上线后调用方手里只有 ``base_url``、一把 ``sk-xc-`` key 和一个 ``model`` 字符串。
下面每一条都对应其中一环：改动它就等于让某个已经在跑的调用方突然坏掉，或者更糟——
静默地换了语义而不报错。

要真的需要改，走 docs/开发计划.md §3.12 的契约号协商流程，别直接改这里的期望值。
"""

from __future__ import annotations

import pytest

from xingcha import contract as C
from xingcha.contract import ErrorType, ModelKind, ModelRefInvalid

# =============================================================================
# 路径归属
# =============================================================================


class TestPathOwnership:
    def test_own_v1_paths_is_exactly_this_set(self):
        """往这个集合里加一项 = 从反代手里收回一条路径 = 破坏性变更。

        新增自有端点请落在 /v1/xc/* 下。
        """
        assert frozenset({"models", "chat/completions"}) == C.OWN_V1_PATHS

    def test_reserved_prefix_never_changes(self):
        assert C.RESERVED_V1_PREFIX == "xc"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("models", "models"),
            ("models/", "models"),
            ("/models", "models"),
            ("//models//", "models"),
            ("chat//completions/", "chat/completions"),
            ("", ""),
            ("/", ""),
        ],
    )
    def test_normalization(self, raw: str, expected: str):
        assert C.normalize_v1_path(raw) == expected

    def test_trailing_slash_models_is_still_ours(self):
        """GET /v1/models/ 必须仍然是自有路径。

        不归一化的话它会落进 catch-all 被反代出去——客户端拿到 200、拿到 400 多个
        上游模型、一个 Agent 都看不到，而且没有任何报错。这是上线第一天就存在的
        静默 bug，且按演进规则事后拦回来算破坏性变更。
        """
        assert C.is_own_v1_path("models/")
        assert C.is_own_v1_path("models")

    def test_case_is_significant(self):
        """一律区分大小写。/v1/Models 不是自有路径。"""
        assert not C.is_own_v1_path("Models")

    def test_retrieve_model_single_segment_is_ours(self):
        """OpenAI 标准的 retrieve-model。客户端用它验证模型是否存在。

        不列为自有路径就归反代 —— 客户端拿 Agent slug 去问会打到 OpenRouter 拿回
        上游 404，据此判定「这个模型不存在」，而事后收回算破坏性变更 = 永久坏掉。
        """
        assert C.is_own_v1_path("models/extract")
        assert C.is_own_v1_path("models/extract/")

    def test_retrieve_model_multi_segment_goes_upstream(self):
        """多段的一律属于上游：上游 model id 含 /，且 OpenRouter 自己有
        /v1/models/{author}/{slug}/endpoints。"""
        assert not C.is_own_v1_path("models/openai/gpt-5")
        assert not C.is_own_v1_path("models/openai/gpt-5/endpoints")

    def test_reserved_namespace_never_proxied(self):
        assert C.is_own_v1_path("xc")
        assert C.is_own_v1_path("xc/anything/at/all")

    @pytest.mark.parametrize(
        "path", ["embeddings", "completions", "generation", "key", "credits", "images/generations"]
    )
    def test_everything_else_is_proxied(self, path: str):
        assert not C.is_own_v1_path(path)

    def test_options_is_never_proxied(self):
        """否则浏览器客户端的 CORS 预检由 OpenRouter 的策略决定，而星槎自己的响应
        又不带 CORS 头 —— 表现为「非流式偶尔能用、浏览器直连必挂」。"""
        assert C.OPTIONS_ALWAYS_OWN is True


# =============================================================================
# token 格式
# =============================================================================


class TestTokenEnvelope:
    def test_scheme_1_roundtrip(self):
        kid = "a1b2c3d4e5f60718"
        secret = "x" * C.TOKEN_SCHEME_1_SECRET_LEN
        token = f"sk-xc-1-{kid}-{secret}"
        m = C.TOKEN_ENVELOPE_RE.match(token)
        assert m is not None
        assert m.group("scheme") == "1"
        assert m.group("kid") == kid
        assert m.group("secret") == secret

    def test_scheme_1_total_length_is_68(self):
        """6 (sk-xc-) + 1 (scheme) + 1 (-) + 16 (kid) + 1 (-) + 43 (secret) = 68.

        算术单独断言一次：把契约写成代码是对的，但没人跑一遍算术的话，
        冻结的就是个错数。
        """
        expected = len(C.TOKEN_PREFIX) + 1 + 1 + C.TOKEN_KID_LEN + 1 + C.TOKEN_SCHEME_1_SECRET_LEN
        assert expected == 68
        token = f"sk-xc-1-{'a' * C.TOKEN_KID_LEN}-{'x' * C.TOKEN_SCHEME_1_SECRET_LEN}"
        assert len(token) == 68
        assert C.TOKEN_ENVELOPE_RE.match(token)

    @pytest.mark.parametrize(
        "bad",
        [
            "sk-xc-1-tooshort-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",  # kid 不足 16
            "sk-xc-0-a1b2c3d4e5f60718-" + "x" * 43,  # scheme 不能是 0
            "sk-xc-1-A1B2C3D4E5F60718-" + "x" * 43,  # kid 必须小写
            "sk-xc-1-a1b2c3d4e5f60718-short",  # secret 过短
            "sk-or-v1-abcdef",  # 上游的 key 形状
            "sk-xc-a1b2c3d4e5f60718-" + "x" * 43,  # 缺 scheme 段
            "",
        ],
    )
    def test_rejects_malformed(self, bad: str):
        assert C.TOKEN_ENVELOPE_RE.match(bad) is None

    def test_secret_length_is_a_range_not_fixed(self):
        """secret 长度按 scheme 可变，所以信封正则里必须是范围。

        这两件事互斥过：如果把总长写死，scheme=2 换个更长的 secret 就进不来了。
        """
        kid = "a" * C.TOKEN_KID_LEN
        assert C.TOKEN_ENVELOPE_RE.match(f"sk-xc-2-{kid}-{'y' * 86}")
        assert C.TOKEN_ENVELOPE_RE.match(f"sk-xc-2-{kid}-{'y' * 16}")

    def test_display_prefix_leaks_no_secret(self):
        """对外显示的标识里不能有秘密本体的任何字符。

        用「明文前 N 字符」当 prefix 的做法会把 secret 的开头印在 UI、日志和
        token list 里。
        """
        kid = "a1b2c3d4e5f60718"
        secret = "S3CR3T" + "x" * 37
        shown = C.token_display_prefix(1, kid)
        assert shown == "sk-xc-1-a1b2c3d4e5f60718"
        assert secret[:6] not in shown

    def test_all_historical_schemes_stay_supported(self):
        """只增不删。删掉一个 scheme = 让已签发的 key 全部失效。"""
        assert 1 in C.TOKEN_SCHEMES_SUPPORTED

    def test_auth_only_via_bearer_header(self):
        assert C.AUTH_HEADER == "authorization"
        assert C.AUTH_SCHEME == "bearer"


# =============================================================================
# model 命名空间分派 —— 最不能改的一条
# =============================================================================


class TestModelDispatch:
    @pytest.mark.parametrize(
        "model",
        [
            "openai/gpt-5",
            "anthropic/claude-opus-4",
            "z-ai/glm-5.3-flash:batch",
            "meta/llama-4:free",
        ],
    )
    def test_slash_means_upstream(self, model: str):
        ref = C.classify_model(model)
        assert ref.kind is ModelKind.UPSTREAM
        assert ref.value == model
        assert ref.explicit is False

    @pytest.mark.parametrize("model", ["extract", "summarize-ticket", "a1", "triage-v2"])
    def test_no_slash_means_agent(self, model: str):
        ref = C.classify_model(model)
        assert ref.kind is ModelKind.AGENT
        assert ref.value == model

    def test_explicit_namespace(self):
        a = C.classify_model("xc:agent/extract")
        assert (a.kind, a.value, a.explicit) == (ModelKind.AGENT, "extract", True)
        m = C.classify_model("xc:model/openai/gpt-5")
        assert (m.kind, m.value, m.explicit) == (ModelKind.UPSTREAM, "openai/gpt-5", True)

    def test_explicit_namespace_uses_colon_not_slash(self):
        """xc: 让显式命名空间在结构上不可能与上游的 vendor/model 混淆。

        用 xc/agent/x 的话它长得就像一个上游 model id，只能靠规则顺序才不撞 ——
        能靠形状区分就不要靠顺序区分。
        """
        assert C.EXPLICIT_NS == "xc:"
        # 歧义的具体样子：若命名空间是 "xc/"，那么 "xc/agent" 与一个上游的
        # vendor/model 在形状上完全一样，只能靠规则顺序才不撞。
        assert C.classify_model("xc/agent").kind is ModelKind.UPSTREAM
        # 而用冒号时，"xc:" 开头的一律先进显式分支，形状上不可能与上游混淆。
        assert C.classify_model("xc:agent/extract").kind is ModelKind.AGENT

    def test_agent_miss_never_falls_back_upstream(self):
        """classify 只做形状判定；查不到 Agent 由调用方回 404。

        绝不能"查不到就当上游模型转发出去" —— 那样一个拼错的 slug 会静默变成一次
        真实的付费调用，而调用方以为自己在调 Agent。
        """
        ref = C.classify_model("nonexistent-agent")
        assert ref.kind is ModelKind.AGENT  # 不是 UPSTREAM

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "UPPER",  # 大写
            "_leading",  # 下划线
            "has.dot",  # 点
            "trailing-",  # 尾连字符
            "-leading",  # 首连字符
            "double--dash",
            "9start",  # 数字开头
            "a",  # 太短
            "x" * 49,  # 太长
        ],
    )
    def test_invalid_slugs_rejected(self, bad: str):
        with pytest.raises(ModelRefInvalid):
            C.classify_model(bad)

    @pytest.mark.parametrize("word", ["models", "me", "health", "version", "xc", "admin", "api"])
    def test_reserved_words_rejected(self, word: str):
        with pytest.raises(ModelRefInvalid):
            C.classify_model(word)

    def test_reserved_prefix_rejected(self):
        with pytest.raises(ModelRefInvalid):
            C.classify_model("xc-builtin")

    def test_slug_charset_may_only_narrow(self):
        """放宽字符集会让原本 404 的字符串突然变成有效 Agent —— 行为的静默改变。

        这条断言把当前字符集钉住：改这个正则时你会看到这个测试。
        """
        assert C.SLUG_RE.pattern == r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$"
        assert (C.SLUG_MIN_LEN, C.SLUG_MAX_LEN) == (2, 48)

    def test_slug_can_never_contain_slash_or_colon(self):
        """这是分派规则成立的前提：含 / 归上游、含 : 归显式命名空间。"""
        for ch in "/:":
            assert not C.SLUG_RE.match(f"ab{ch}cd")


# =============================================================================
# 请求字段三态
# =============================================================================


class TestRequestFieldTables:
    def test_three_tables_are_disjoint(self):
        assert not (C.REQUEST_HONOR & C.REQUEST_IGNORE)
        assert not (C.REQUEST_HONOR & C.REQUEST_REJECT)
        assert not (C.REQUEST_IGNORE & C.REQUEST_REJECT)

    def test_user_is_permanently_meaningless(self):
        """元规则：列入 ignore 的字段永久无语义，永不 honor。

        user 是 OpenAI 语义里天然的租户位，v2 做多用户时一定会想拿它当 subject ——
        那一刻所有在 v1 往 user 里塞了任意字符串的调用方，行为全部改变。
        租户归属永远只来自 token。
        """
        assert "user" in C.REQUEST_IGNORE
        assert "user" not in C.REQUEST_HONOR

    def test_session_id_rejected_not_ignored(self):
        """不走"先忽略、以后支持"：同一请求在两个版本里两种语义是无法回退的毁约。

        将来要支持就叫 x_xingcha.session_id，或把这个 400 放宽成 200（放宽是加法）。
        """
        assert "session_id" in C.REQUEST_REJECT
        assert "session_id" not in C.REQUEST_IGNORE

    @pytest.mark.parametrize("field", ["retries", "max_retries", "usage_limits", "response_format"])
    def test_guardrail_overrides_are_rejected(self, field: str):
        """实测 run(retries=) 与 run(spec=) 都能覆盖 Agent 构造时的值。

        不拦住等于让调用方自行放大重试预算、绕过费用护栏。response_format 同理 ——
        让调用方覆盖输出形状会让「200 即符合 schema」这个承诺失效。
        """
        assert field in C.REQUEST_REJECT


# =============================================================================
# 响应形状
# =============================================================================


class TestResponseShape:
    def test_single_extension_namespace(self):
        assert C.EXT_KEY == "x_xingcha"
        assert C.EXT_SHAPE_VERSION == 1

    def test_content_is_always_a_string(self):
        """永不把 dict 直接放进 content —— 会让所有按 str 处理 content 的客户端崩掉。"""
        assert C.CONTENT_ALWAYS_STR is True

    def test_cost_is_a_string_not_a_number(self):
        """float 存不住 Decimal；且 null（无法定价）必须与真实的 0 费用可区分 ——
        实测约 1/3 的在售模型在 genai-prices 查不到价。"""
        assert C.COST_AS_STRING is True

    def test_usage_is_whole_run(self):
        """一次 200 背后可能有 1+retries 次模型调用。

        等发现"一次调用怎么花了 4 倍"时，最自然的"修正"是只报最后一次尝试 ——
        那会让所有基于 usage 的账单核对、配额聚合、成本看板同时改变口径，
        是无法回退的数值毁约。
        """
        assert C.USAGE_IS_WHOLE_RUN is True
        assert C.USAGE_ON_ERROR is True

    def test_sse_frame_order_and_terminator(self):
        """伪流式与真流式用完全相同的帧形状，所以 v0.4 换成真 delta 时客户端不可见。"""
        assert C.SSE_FRAME_ORDER == ("role", "content", "finish", "summary", "done")
        assert C.SSE_DONE == "data: [DONE]\n\n"


# =============================================================================
# 错误契约
# =============================================================================


class TestErrorContract:
    def test_every_type_has_a_status(self):
        assert set(ErrorType) == set(C.ERROR_HTTP_STATUS)

    def test_status_codes_never_change(self):
        assert C.ERROR_HTTP_STATUS == {
            ErrorType.INVALID_API_KEY: 401,
            ErrorType.QUOTA_EXCEEDED: 429,
            ErrorType.MODEL_NOT_FOUND: 404,
            ErrorType.MODEL_INVALID: 400,
            ErrorType.PARAM_UNSUPPORTED: 400,
            ErrorType.STREAM_UNSUPPORTED: 400,
            ErrorType.REQUEST_TOO_LARGE: 413,
            ErrorType.SCHEMA_VIOLATION: 422,
            ErrorType.AGENT_SPEC_INVALID: 400,
            ErrorType.AGENT_BUILD_FAILED: 500,
            ErrorType.UPSTREAM_ERROR: 502,
            ErrorType.UPSTREAM_TIMEOUT: 504,
            ErrorType.REQUEST_TIMEOUT: 504,
            ErrorType.INTERNAL_ERROR: 500,
        }

    def test_spec_invalid_and_build_failed_are_separate(self):
        """用户填错（400）和上游版本变动（500）是两个完全不同的处置路径。

        一码两 HTTP 会让调用方无法分支。
        """
        assert C.ERROR_HTTP_STATUS[ErrorType.AGENT_SPEC_INVALID] == 400
        assert C.ERROR_HTTP_STATUS[ErrorType.AGENT_BUILD_FAILED] == 500

    def test_two_kinds_of_timeout_are_separate(self):
        """单次上游请求超时（ModelAPIError）与整轮墙钟超时（asyncio.timeout）
        来源不同、排查路径也不同。"""
        assert ErrorType.UPSTREAM_TIMEOUT != ErrorType.REQUEST_TIMEOUT

    def test_auth_failures_are_indistinguishable(self):
        """区分 token 无效/禁用/过期 = 给公网一个 token 有效性 oracle。"""
        assert C.AUTH_ERRORS_INDISTINGUISHABLE is True


# =============================================================================
# 直通层卫生 —— 安全关键集合
# =============================================================================


class TestPassthroughHygiene:
    def test_passthrough_always_requires_auth(self):
        """一个不鉴权的 catch-all 反代 + 一把付费 key = 开放代理，
        是本项目唯一的「一天烧光余额」级事故。"""
        assert C.PASSTHROUGH_REQUIRES_AUTH is True

    @pytest.mark.parametrize(
        "header",
        ["x-forwarded-for", "x-real-ip", "forwarded", "cf-connecting-ip", "true-client-ip"],
    )
    def test_all_client_ip_headers_stripped(self, header: str):
        """只剥 XFF 是不够的：Forwarded 与 CF-Connecting-IP 同样会把真实来源交给上游，
        中转形同白建。"""
        assert header in C.STRIP_REQUEST_HEADERS

    def test_client_credentials_never_forwarded(self):
        assert "authorization" in C.STRIP_REQUEST_HEADERS
        assert "cookie" in C.STRIP_REQUEST_HEADERS

    def test_response_headers_are_an_allowlist(self):
        """必须是白名单：黑名单式只剥 hop-by-hop 的话，上游的 Set-Cookie 会落在
        你自己的域上，任何 echo/debug 头也一并出去。"""
        assert "set-cookie" not in C.ALLOW_RESPONSE_HEADERS
        assert "content-type" in C.ALLOW_RESPONSE_HEADERS
        # 白名单必须是闭集，不是"除了这些以外都放行"
        assert len(C.ALLOW_RESPONSE_HEADERS) < 20

    def test_v1_has_no_quota_on_passthrough_and_says_so(self):
        """v1 不做配额是已对齐的决定，但必须显式记录 —— 不能让人误以为有费用护栏。"""
        assert C.PASSTHROUGH_ENFORCES_QUOTA is False


# =============================================================================
# 运行护栏
# =============================================================================


class TestGuardrails:
    def test_body_limit_is_frozen(self):
        """事后调小是破坏性变更（原本能过的请求突然 413）。"""
        assert C.MAX_BODY_BYTES == 8 * 1024 * 1024

    def test_redos_keywords_forbidden(self):
        """pattern 由 jsonschema 用 Python re 在事件循环上执行，且每次 schema 重试
        都重跑一遍。一条 (a+)+$ 就能把一核打满，整个单进程服务停摆。"""
        assert "pattern" in C.SCHEMA_FORBIDDEN_KEYWORDS
        assert "patternProperties" in C.SCHEMA_FORBIDDEN_KEYWORDS

    def test_only_local_refs_allowed(self):
        """jsonschema 在未给定封闭 registry 时会真的去取非本地 $ref ——
        {"$ref": "http://attacker/x.json"} 是一个校验期 SSRF。"""
        assert C.SCHEMA_REF_ALLOWED_PREFIX == "#/"

    def test_single_worker_is_asserted_not_suggested(self):
        """进程级 ConcurrencyLimiter、内存用量缓冲、SQLite 单写者全都依赖它。

        改成 2 会同时静默打破上游并发封顶、丢一半用量缓冲、并引入
        database is locked —— 三个症状互不相关，排查成本极高。
        """
        assert C.REQUIRED_WORKERS == 1

    def test_wal_is_required(self):
        """bind mount 落在网络盘上时 WAL 会静默降级，症状是零星的 database is locked
        —— 最难查的一类问题。宁可起不来。"""
        assert C.REQUIRED_JOURNAL_MODE == "wal"

    def test_file_modes(self):
        """共享 VPS 上 0644 的库文件等于把 token hash 与 Fernet 密文交给任意本地账号。"""
        assert (C.DIR_MODE, C.FILE_MODE, C.UMASK) == (0o700, 0o600, 0o077)


# =============================================================================
# 计量与档位
# =============================================================================


class TestMeteringContract:
    def test_cost_source_has_four_states_from_day_one(self):
        """upstream 在 v1 拿不到，但取值必须从第一天就存在 —— 否则 v0.4 加对账时
        要改一次 CHECK 约束。"""
        assert {s.value for s in C.CostSource} == {
            "openrouter_catalog",
            "genai_prices",
            "upstream",
            "unknown",
        }

    def test_all_four_tiers_reserved_though_only_t2_implemented(self):
        """四档从第一天就进 DB 的 CHECK。没预留的话，补 T1 时就是一次重建表的迁移。"""
        assert {t.value for t in C.Tier} == {"T1", "T2", "T1P", "T3"}

    def test_tier_detection_uses_structured_outputs_only(self):
        """实测今天 424 个模型里 response_format 365 个、structured_outputs 340 个 ——
        有 25 个只有前者。混用会把 T2 误判成 T1，于是对用户谎称「有原生保证」。
        """
        assert C.CATALOG_NATIVE_SCHEMA_PARAM == "structured_outputs"


# =============================================================================
# 契约本身
# =============================================================================


def test_contract_version_is_one():
    """契约号只在发生破坏性变更时 +1，而破坏性变更本身应当几乎不发生。"""
    assert C.CONTRACT_VERSION == 1


def test_features_only_grow():
    """一个特性从缺席变存在是加法；反向是破坏性变更。"""
    assert {"passthrough", "agents", "structured_output"} <= C.FEATURES


def test_contract_module_has_no_internal_imports():
    """契约处在依赖图最底层，不 import 任何 xingcha 模块（开发计划 §6 标准 1）。

    一旦它 import 了别的模块，就会出现"契约依赖实现"的倒挂，而契约必须是实现去
    对齐的对象。
    """
    import pathlib

    src = pathlib.Path(C.__file__).read_text(encoding="utf-8")
    for line in src.splitlines():
        s = line.strip()
        if s.startswith(("import ", "from ")):
            assert "xingcha" not in s, f"contract.py 不应依赖 xingcha 内部模块：{s}"


def test_contract_doc_is_in_sync():
    """docs/CONTRACT.md 必须与常量一致。

    文档不手写——手写的契约文档一定会和代码漂移，而漂移之后你就有了两份互相矛盾的
    「权威」，更糟的是人会去信文档而不是代码。改了常量忘了重新生成，这条会变红：

        python -m xingcha.contract_doc
    """
    from xingcha.contract_doc import DOC_PATH, render

    assert DOC_PATH.exists(), f"{DOC_PATH} 不存在，跑一次 python -m xingcha.contract_doc"
    assert DOC_PATH.read_text(encoding="utf-8") == render(), (
        "docs/CONTRACT.md 与 contract.py 不一致。重新生成：python -m xingcha.contract_doc"
    )
