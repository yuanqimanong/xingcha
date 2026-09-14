"""星槎对外契约的唯一定义处。

所有正则、闭集、字段清单只在这里定义，别处只许引用。本模块处在依赖图最底层，
不 import 任何 xingcha 模块。

调用方手里只有 ``base_url``、一把 ``sk-xc-`` key、一个 ``model`` 字符串。凡是会
打断这三样的改动都必须上线前定死，此后只能加、不能改；各常量下的「演进规则」说明
允许怎么扩展。``tests/test_contract_frozen.py`` 守着这些值——它变红不是测试坏了，
是你正在做破坏性变更，走 §12 的契约号协商。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

# =============================================================================
# 契约版本
# =============================================================================

#: 对外契约版本。通过 ``X-Xingcha-Contract`` 请求/响应头双向协商。
#:
#: 不是软件版本（那是 ``xingcha.__version__``）。只在破坏性变更时 +1，为的是万一
#: 真要收紧某个行为（例如给直通路径加配额闸），有一条非硬切的发布通道。
CONTRACT_VERSION: Final = 1

#: 能力位。随 ``GET /version`` 返回，让调用方无需试探即可知道服务端支持什么。
#:
#: 演进规则：只增不删。一个特性从 False 变 True 是加法；反向是破坏性变更。
FEATURES: Final[frozenset[str]] = frozenset(
    {
        "passthrough",  # /v1 下非自有路径透明反代到上游
        "agents",  # Agent 以 slug 作为 model id 调用
        "structured_output",  # 结构化输出保证（T2 档）
        "streaming_passthrough",  # 直通路径的流式转发
        "streaming_agents",  # 纯文本 Agent 的真 delta 流式（结构化 Agent 仍为 400）
        "quota",  # 三级主体 × 三窗口的配额执行（Agent 路径）
    }
)

#: 直通路径的配额执行默认关闭，由管理员显式打开。
#:
#: §8 把 ``PASSTHROUGH_ENFORCES_QUOTA`` 冻结成 False：给直通层加闸是收紧。所以能力
#: 做好、默认关，打开后 ``/version`` 的 features 多一项 ``quota_passthrough``——
#: 对既有调用方就不是静默的行为改变，而是部署者的显式决定。
FEATURE_QUOTA_PASSTHROUGH: Final = "quota_passthrough"


# =============================================================================
# 1 · 路径归属
# =============================================================================

#: 对外的四个前缀。除此之外不暴露任何路径。
PUBLIC_PREFIXES: Final[tuple[str, ...]] = ("/v1", "/api/v1", "/admin", "/healthz")

#: 星槎在 ``/v1`` 下自己实现的路径（相对 ``/v1/``，已归一化）。闭集。
#:
#: 其余一切 ``/v1`` 路径字节级反代到上游，所以往这里加一项 = 从反代收回一条路径 =
#: 破坏性变更。演进规则：新增自有端点只能落在 ``/v1/xc/*``（见 RESERVED_V1_PREFIX），
#: 该前缀从不反代，加东西不会从任何人手上拿走什么。
OWN_V1_PATHS: Final[frozenset[str]] = frozenset(
    {
        "models",
        "chat/completions",
    }
)

#: 星槎的永久保留命名空间（相对 ``/v1/``）。从不反代，即使现在还没有任何实现。
#:
#: 这是唯一能在不破坏兼容的前提下新增自有端点的地方。
RESERVED_V1_PREFIX: Final = "xc"

#: ``GET /v1/models/{id}`` 也是自有路径，但只在 id 为单段时。
#:
#: 它是 OpenAI 标准的 retrieve-model，Cherry Studio / Continue 一类客户端拿它验证
#: 模型存在。归反代的话，客户端拿 Agent slug 去问会打到上游、拿回 404，判定模型不
#: 存在；而事后从反代收回算破坏性变更，等于这个端点永久坏掉。
#:
#: 限定单段：Agent slug 永不含 ``/``（见 SLUG_RE），上游 id 一定含 ``/``，多段的
#: （如 ``/v1/models/{author}/{slug}/endpoints``）全属上游，留给反代才正确。
MODELS_ITEM_SEGMENTS: Final = 1

_MULTI_SLASH_RE: Final = re.compile(r"/+")


def normalize_v1_path(rel_path: str) -> str:
    """把 ``/v1/`` 之后的路径归一化成用于闭集匹配的形式。

    折叠重复斜杠、去掉首尾斜杠，大小写敏感。

    没有这一步会有一个静默 bug：``GET /v1/models/`` 带尾斜杠时 FastAPI 的
    ``redirect_slashes`` 在有 catch-all 的情况下不生效，请求直接被反代出去——
    客户端拿到 200、拿到一堆上游模型、一个 Agent 都看不到，且不报错。
    """
    return _MULTI_SLASH_RE.sub("/", rel_path).strip("/")


def is_own_v1_path(rel_path: str) -> bool:
    """``/v1/<rel_path>`` 是否由星槎自己处理（否则反代到上游）。

    注意 ``OPTIONS`` 不走这里：任何 ``/v1`` 路径的 ``OPTIONS`` 都由星槎应答，
    永不反代（见 OPTIONS_ALWAYS_OWN）。
    """
    p = normalize_v1_path(rel_path)
    if p in OWN_V1_PATHS:
        return True
    if p == RESERVED_V1_PREFIX or p.startswith(f"{RESERVED_V1_PREFIX}/"):
        return True
    # GET /v1/models/{id}，且仅限单段
    if p.startswith("models/"):
        return p.count("/") == MODELS_ITEM_SEGMENTS
    return False


#: 任何 ``/v1`` 路径的 OPTIONS 一律由星槎应答，永不反代。
#:
#: 否则浏览器客户端（Open WebUI、自建前端）直连时，CORS 预检由上游策略决定而星槎
#: 自己的响应不带 CORS 头，表现为"浏览器直连必挂"；事后再拦 OPTIONS 算破坏性变更。
OPTIONS_ALWAYS_OWN: Final = True


# =============================================================================
# 2 · 鉴权与 token 格式
# =============================================================================

#: 唯一接受的鉴权方式。永不支持 query string 传 key（会进日志、进 Referer、进浏览器
#: 历史），永不支持 ``api-key`` / ``x-api-key`` 头。
AUTH_HEADER: Final = "authorization"
AUTH_SCHEME: Final = "bearer"  # 比对时大小写不敏感

#: token 明文信封。``sk-xc-<scheme>-<kid>-<secret>``
#:
#: - ``scheme`` —— 哈希算法分派位。换算法 = 新 scheme 数字，历史 scheme 的校验分支
#:   永久保留，已签发的 key 不重签、不失效。
#: - ``kid`` —— 唯一查表键，与 secret 无关、不可推导。拿 hash 本身当查表键的话，
#:   将来换带盐的 argon2id 就无法反查，只能全表逐行 verify（每请求 ``O(n)`` 次
#:   argon2 = 送上门的 DoS），"已签发 key 永不失效"当场破掉。它同时让对外显示的
#:   前缀不含秘密本体——用明文前 N 字符当 prefix 会把秘密印进 UI 与日志。
#: - ``secret`` —— 真正的随机部分。长度按 scheme 可变，所以这里是范围而非定长。
TOKEN_ENVELOPE_RE: Final = re.compile(
    r"^sk-xc-(?P<scheme>[1-9][0-9]{0,2})-(?P<kid>[0-9a-z]{16})-(?P<secret>[A-Za-z0-9_-]{16,86})$"
)

TOKEN_PREFIX: Final = "sk-xc-"
TOKEN_KID_LEN: Final = 16

#: scheme=1：secret 为 ``secrets.token_urlsafe(32)``（43 字符），校验用常量时间比较
#: ``sha256(secret)`` 的十六进制。
TOKEN_SCHEME_CURRENT: Final = 1
TOKEN_SCHEME_1_SECRET_LEN: Final = 43
TOKEN_SCHEME_1_ALG: Final = "sha256"

#: 服务端必须永久保留校验能力的全部 scheme。**只增不删。**
TOKEN_SCHEMES_SUPPORTED: Final[frozenset[int]] = frozenset({1})


def token_display_prefix(scheme: int, kid: str) -> str:
    """UI / 日志 / ``token list`` 里展示的标识。不含秘密本体的任何字符。"""
    return f"{TOKEN_PREFIX}{scheme}-{kid}"


# =============================================================================
# 3 · model 命名空间与分派
# =============================================================================

#: Agent slug。禁含 ``/`` ``:`` ``.`` ``_`` 与大写字母。
#:
#: 演进规则：字符集只能收紧。放宽会让原本 404 的字符串突然变成有效 Agent。
SLUG_RE: Final = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
SLUG_MIN_LEN: Final = 2
SLUG_MAX_LEN: Final = 48

#: 保留字：会与端点路径段或显式命名空间混淆的词。
SLUG_RESERVED: Final[frozenset[str]] = frozenset(
    {"models", "me", "health", "healthz", "readyz", "version", "xc", "admin", "api"}
)

#: 保留前缀：留给星槎将来可能内置的 Agent。
SLUG_RESERVED_PREFIX: Final = "xc-"

#: 隐式上游裸模型 id：一定含 ``/``（``vendor/name``），可带 ``:free`` / ``:batch``
#: 后缀。不能放宽——含不含 ``/`` 正是隐式分派的判据（见 classify_model）。
UPSTREAM_MODEL_RE: Final = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+(:[A-Za-z0-9._-]+)?$")

#: 显式上游模型 id（``xc:model/<id>``）：不要求含 ``/``。
#:
#: 上游可切换，而聚合方（OpenRouter / 硅基流动 / Together）的 id 含斜杠、直连厂商
#: （DeepSeek / Moonshot / Groq / 智谱）不含。只认含斜杠的话，切到直连厂商后直通
#: 完全不可用——不含斜杠的名字会被当成 Agent slug，返回 model_not_found。
#:
#: 隐式规则绝不跟着放宽：它守的是"拼错的 slug 不能静默变成一次真实的付费调用"。
#: 放宽只发生在调用方显式写了 ``xc:model/`` 时，那一刻意图没有歧义。纯加法。
EXPLICIT_UPSTREAM_MODEL_RE: Final = re.compile(r"^[A-Za-z0-9._:/-]{1,200}$")

#: 星槎显式命名空间前缀。用 ``xc:`` 而不是 ``xc/``：冒号让它在结构上不可能与上游的
#: ``vendor/model`` 混淆。``xc/agent/extract`` 只能靠规则顺序才不撞，形状能区分就
#: 不要靠顺序区分。
EXPLICIT_NS: Final = "xc:"
EXPLICIT_KIND_AGENT: Final = "agent"
EXPLICIT_KIND_MODEL: Final = "model"
EXPLICIT_KINDS: Final[frozenset[str]] = frozenset({EXPLICIT_KIND_AGENT, EXPLICIT_KIND_MODEL})


class ModelKind(StrEnum):
    AGENT = "agent"
    UPSTREAM = "upstream"


@dataclass(frozen=True, slots=True)
class ModelRef:
    """``model`` 字段的解析结果。"""

    kind: ModelKind
    value: str
    #: 是否经由 ``xc:`` 显式命名空间指定（影响错误消息，不影响路由）
    explicit: bool = False


class ModelRefInvalid(ValueError):
    """``model`` 字段形状非法。映射到 400 ``model_invalid``。"""


def classify_model(model: str) -> ModelRef:
    """把请求里的 ``model`` 字段解析成 Agent 引用或上游模型引用。

    整个星槎唯一的路由分派点，也是最不能改的规则——它编码在每个调用方的 model
    字符串里。按顺序三条，无例外：

    1. 以 ``xc:`` 开头 → 显式命名空间（``xc:agent/<slug>`` 或 ``xc:model/<上游 id>``）
    2. 否则含 ``/`` → 上游裸模型 id，原样透传
    3. 其余 → Agent slug；查不到直接 404，绝不猜测性地转发给上游

    第 1 条不要求含 ``/``（直连厂商的 id 没有斜杠），第 2 条必须要求，否则第 3 条
    无从判断。第 3 条绝不回落：查不到就当上游模型转发的话，一个拼错的 slug 会静默
    变成一次真实的付费调用。
    """
    if not model or not isinstance(model, str):
        raise ModelRefInvalid("model 不能为空")

    if model.startswith(EXPLICIT_NS):
        rest = model[len(EXPLICIT_NS) :]
        kind, sep, value = rest.partition("/")
        if not sep or not value:
            raise ModelRefInvalid(
                f"显式命名空间的写法是 {EXPLICIT_NS}agent/<slug> 或 {EXPLICIT_NS}model/<上游 id>"
            )
        if kind == EXPLICIT_KIND_AGENT:
            validate_slug(value)
            return ModelRef(ModelKind.AGENT, value, explicit=True)
        if kind == EXPLICIT_KIND_MODEL:
            # 显式通道不要求含 ``/``：直连厂商的 id 没有斜杠。见
            # EXPLICIT_UPSTREAM_MODEL_RE 的说明。
            if not EXPLICIT_UPSTREAM_MODEL_RE.match(value):
                raise ModelRefInvalid(f"不是合法的上游 model id：{value!r}")
            return ModelRef(ModelKind.UPSTREAM, value, explicit=True)
        raise ModelRefInvalid(
            f"未知的命名空间 {EXPLICIT_NS}{kind}/，合法值：{sorted(EXPLICIT_KINDS)}"
        )

    if "/" in model:
        if not UPSTREAM_MODEL_RE.match(model):
            raise ModelRefInvalid(f"不是合法的上游 model id：{model!r}")
        return ModelRef(ModelKind.UPSTREAM, model)

    validate_slug(model)
    return ModelRef(ModelKind.AGENT, model)


def validate_slug(slug: str) -> None:
    """校验 Agent slug，不合法则抛 :class:`ModelRefInvalid`。"""
    if not (SLUG_MIN_LEN <= len(slug) <= SLUG_MAX_LEN):
        raise ModelRefInvalid(
            f"Agent 标识长度须在 {SLUG_MIN_LEN}–{SLUG_MAX_LEN} 之间，收到 {len(slug)}"
        )
    if not SLUG_RE.match(slug):
        raise ModelRefInvalid(
            f"Agent 标识 {slug!r} 不合法：只允许小写字母、数字与连字符，且须以字母开头"
        )
    if slug in SLUG_RESERVED:
        raise ModelRefInvalid(f"{slug!r} 是保留字")
    if slug.startswith(SLUG_RESERVED_PREFIX):
        raise ModelRefInvalid(f"{SLUG_RESERVED_PREFIX!r} 是保留前缀")


# =============================================================================
# 4 · GET /v1/models 的形状与顺序
# =============================================================================

OWNED_BY_XINGCHA: Final = "xingcha"
OWNED_BY_UPSTREAM: Final = "openrouter"
OWNED_BY_VALUES: Final[frozenset[str]] = frozenset({OWNED_BY_XINGCHA, OWNED_BY_UPSTREAM})

#: 列表顺序：Agent 行（按 created_at 升序）在前，上游行（按 catalog 原序）在后，
#: 按 id 去重且 Agent 优先。顺序必须冻结——部分客户端取 ``data[0]`` 当默认模型。
MODELS_AGENTS_FIRST: Final = True

#: catalog 拉取失败或过期且刷新失败时：返回上次成功的快照，并在 ``x_xingcha`` 里标
#: ``catalog_stale=true`` 与 ``fetched_at``。
#:
#: 降级语义必须冻结——客户端会缓存这个列表并把 id 写进会话配置。只返回 Agent 行会
#: 静默抹掉用户配置里的上游模型；返回 502 又可能让客户端判定整个端点不可用。
MODELS_STALE_WHILE_ERROR: Final = True


# =============================================================================
# 5 · 请求字段的三态：honor / ignore / reject
# =============================================================================

#: 会真正生效的字段。
REQUEST_HONOR: Final[frozenset[str]] = frozenset(
    {
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
)

#: 接受但永久无语义的字段。
#:
#: 元规则（这条本身也是契约）：列入 ignore 的字段永久无语义，永不 honor；需要新语义
#: 必须用新字段名或走 ``x_xingcha`` 入参对象。典型是 ``user``——它是 OpenAI 语义里
#: 天然的租户位，一旦哪天拿它当 subject，所有往里塞过任意字符串的调用方行为全变
#: （被归到不存在的子账号、撞上别人的配额）。租户归属永远只来自 token。
REQUEST_IGNORE: Final[frozenset[str]] = frozenset({"user", "store", "metadata", "n"})

#: 直接 400 拒绝的字段。
#:
#: 演进规则：reject 表只能缩小。移出去（开始支持）是加法；把字段从 honor/ignore
#: 移进来是收紧，禁止。
#:
#: ``retries`` / ``max_retries`` / ``usage_limits`` 必须在这里：实测 ``run(retries=)``
#: 与 ``run(spec=)`` 都能覆盖 Agent 构造时的值，不拦等于让调用方自行放大重试预算。
#: ``response_format`` 同理——让调用方覆盖输出形状会让"200 即符合 schema"失效。
#: ``session_id`` 直接拒绝而不是先忽略：同一请求在两个版本里两种语义无法回退。
REQUEST_REJECT: Final[frozenset[str]] = frozenset(
    {
        "retries",
        "max_retries",
        "usage_limits",
        "response_format",
        "session_id",
        "tools",
        "tool_choice",
        "functions",
        "function_call",
    }
)


# =============================================================================
# 6 · 响应形状
# =============================================================================

#: 所有星槎自有字段的**唯一**落点。响应体除此之外不加任何非 OpenAI 键。
EXT_KEY: Final = "x_xingcha"

#: ``x_xingcha`` 的形状版本号。
#:
#: 演进规则：往里**增**字段不递增 v；**删字段或改字段语义**必须递增 v，并在一个版本内
#: 同时提供新旧键。
EXT_SHAPE_VERSION: Final = 1

#: 结构化输出的承载形式：``message.content`` 永远是字符串
#: （``json.dumps(dict, ensure_ascii=False)``，不缩进）。调用方 ``json.loads`` 取回 dict。
#:
#: 永不改成把 dict 直接放进 content——按 str 处理 content 的客户端会全崩。要提供
#: 已解析形式只能并行加 ``x_xingcha.parsed``。
CONTENT_ALWAYS_STR: Final = True

#: 金额的 JSON 类型：字符串形式的 Decimal，或 null。不是 number——float 存不住
#: Decimal，而 ``null``（无法定价）与真实的 0 费用必须可区分。
COST_AS_STRING: Final = True

#: ``usage`` 的口径：整轮累计，含全部 schema 重试与工具往返的 token 与费用。
#:
#: 必须冻结。一次 200 背后可能有 ``1 + retries`` 次模型调用；日后改成只报最后一次，
#: 会让账单核对、配额聚合、成本看板同时改变口径。调用方要折算真实产出成本，用
#: ``x_xingcha.schema_retries`` 自行换算。
USAGE_IS_WHOLE_RUN: Final = True

#: 失败响应（429 / 422）也必须带 usage，否则失败 run 的花费不可见。
USAGE_ON_ERROR: Final = True

#: 具体是哪两种错误必须带 usage。闭集，一处定义。
#:
#: 只有这两种背后可能有真实的模型调用（重试耗尽是 1+retries 次；配额超限是跑到
#: 一半被拦）。其余错误（401 / 400 / 413）在打到上游前就返回了。
#:
#: 即使这次真的零调用也要给 0——不给的话调用方读 ``.usage.total_tokens`` 要分情况
#: 处理，形状统一比省几个字节重要。
USAGE_ON_ERROR_TYPES: Final[frozenset[str]] = frozenset(
    {
        "quota_exceeded",
        "schema_violation",
    }
)

#: SSE 终止行。
SSE_DONE: Final = "data: [DONE]\n\n"

#: SSE 帧序列。伪流式与真流式逐字相同，真流式上线时唯一的变化是 ``content`` 帧变多，
#: 对客户端兼容。当初发伪流式而不是 400 就是为此：客户端会为 400 写死绕过逻辑。
#:
#: 中途失败的表达也冻结在这里：200 发出后改不了状态码，所以不发 ``[DONE]`` 就是失败
#: 信号（OpenAI 也是这个行为）。调用方按"流是否以 ``[DONE]`` 结尾"判成败。
SSE_FRAME_ORDER: Final[tuple[str, ...]] = (
    "role",  # {"delta": {"role": "assistant"}}
    "content",  # {"delta": {"content": "..."}}  × N
    "finish",  # {"delta": {}, "finish_reason": "stop"}
    "summary",  # {"choices": [], "usage": {...}, "x_xingcha": {...}}  可选
    "done",  # data: [DONE]
)


# =============================================================================
# 7 · 错误契约
# =============================================================================


class ErrorType(StrEnum):
    """``error.type`` 闭集。供 SDK 做分支判断，粒度刻意保持粗。

    演进规则：只能**新增** type，且新值必须配一个此前未使用的语义。既有 type 的
    HTTP 码永不改动、永不改名、永不复用于别的语义。
    """

    INVALID_API_KEY = "invalid_api_key"
    QUOTA_EXCEEDED = "quota_exceeded"
    MODEL_NOT_FOUND = "model_not_found"
    MODEL_INVALID = "model_invalid"
    PARAM_UNSUPPORTED = "param_unsupported"
    STREAM_UNSUPPORTED = "stream_unsupported"
    REQUEST_TOO_LARGE = "request_too_large"
    SCHEMA_VIOLATION = "schema_violation"
    AGENT_SPEC_INVALID = "agent_spec_invalid"
    AGENT_BUILD_FAILED = "agent_build_failed"
    UPSTREAM_ERROR = "upstream_error"
    UPSTREAM_TIMEOUT = "upstream_timeout"
    REQUEST_TIMEOUT = "request_timeout"
    INTERNAL_ERROR = "internal_error"


#: 每个 error type 的 HTTP 状态码。永不改动。
#:
#: 两处刻意的拆分：``agent_spec_invalid`` (400) vs ``agent_build_failed`` (500)——
#: 用户填错与上游版本变动是两条处置路径；``upstream_timeout`` (单次上游请求) vs
#: ``request_timeout`` (整轮墙钟)——前者走 ``model_settings['timeout']``，后者只能靠
#: ``asyncio.timeout``，来源与排查路径都不同。
ERROR_HTTP_STATUS: Final[dict[ErrorType, int]] = {
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

#: 5xx 对外只给固定文案 + run_id，细节只进日志：``UserError`` / httpx / openai 的
#: 异常文本常带完整 URL、偶尔带 header，回显就是一条上游 key 泄漏路径。
INTERNAL_ERROR_MESSAGE: Final = "服务内部错误。请把 run_id 提供给管理员以便排查。"

#: 对外不区分 token 无效 / 禁用 / 过期，一律 ``invalid_api_key``——区分等于给公网一个
#: token 有效性 oracle。区分只进日志。
AUTH_ERRORS_INDISTINGUISHABLE: Final = True


# =============================================================================
# 8 · 直通层的透明性与卫生
# =============================================================================

#: 转发给上游前必须剥离的请求头。
#:
#: 主体是客户端 IP 类的头，不剥掉就把真实来源交给上游了，中转形同白建。注意这与
#: "客户端 → 星槎"那一跳相反：那一跳需要 XFF/X-Real-IP 才能记录来源。
STRIP_REQUEST_HEADERS: Final[frozenset[str]] = frozenset(
    {
        "x-forwarded-for",
        "x-real-ip",
        "forwarded",
        "x-forwarded-host",
        "x-forwarded-proto",
        "cf-connecting-ip",
        "cf-ipcountry",
        "true-client-ip",
        "x-client-ip",
        # 鉴权头必须换成上游 key，不能把 sk-xc- 透出去
        "authorization",
        "cookie",
        # hop-by-hop
        "connection",
        "keep-alive",
        "transfer-encoding",
        "upgrade",
        "proxy-authorization",
        "proxy-authenticate",
        "te",
        "trailer",
        "host",
        "content-length",
    }
)

#: 回给客户端的上游响应头白名单。不在名单里的一律丢弃。
#:
#: 必须是白名单：黑名单只剥 hop-by-hop 的话，上游的 ``Set-Cookie`` 会落在你自己的
#: 域上，echo/debug 头也一并出去。
ALLOW_RESPONSE_HEADERS: Final[frozenset[str]] = frozenset(
    {
        "content-type",
        "content-encoding",
        "cache-control",
        "x-request-id",
        "retry-after",
        # OpenRouter 的限流头，客户端做退避要用
        "x-ratelimit-limit",
        "x-ratelimit-remaining",
        "x-ratelimit-reset",
    }
)

#: 直通路径强制鉴权：无有效 sk-xc- key 一律 401，绝不转发给上游。不鉴权的 catch-all
#: 反代 + 一把付费 key = 开放代理，是本项目唯一的"一天烧光余额"级事故。
PASSTHROUGH_REQUIRES_AUTH: Final = True

#: v1 的直通路径记 run 行与 token，但不执行配额。唯一真正的钱刹车在上游侧——给那把
#: 上游 key 单独设一个低额信用上限。
PASSTHROUGH_ENFORCES_QUOTA: Final = False


# =============================================================================
# 9 · 运行护栏
# =============================================================================

#: 请求体上限。超过即 413 ``request_too_large``。
#:
#: 直通层把 body 整块缓冲成 bytes（异步迭代器会强制 chunked，部分中转会拒），没有
#: 上限时一个大 POST 就能打死这个单进程。进契约是因为事后调小算破坏性变更。
MAX_BODY_BYTES: Final = 8 * 1024 * 1024

#: 单个 JSON Schema 的上限（schema_guard）。
SCHEMA_MAX_BYTES: Final = 64 * 1024
SCHEMA_MAX_DEPTH: Final = 8
SCHEMA_MAX_PROPS: Final = 120
SCHEMA_MAX_ENUM: Final = 200

#: schema 里被拒绝的关键字。``pattern`` / ``patternProperties`` 由 jsonschema 用
#: Python ``re`` 在事件循环上执行，且每次重试重跑；一条 ``(a+)+$`` 就能打满一核。
SCHEMA_FORBIDDEN_KEYWORDS: Final[frozenset[str]] = frozenset({"pattern", "patternProperties"})

#: 只允许指向文档自身的 ``$ref``。jsonschema 未给定封闭 registry 时会真的去取远程
#: ``$ref``，那是校验期 SSRF。除这条前缀检查外，构造 validator 还必须传入空的
#: ``referencing.Registry``，让远程取回在结构上不可能发生。
SCHEMA_REF_ALLOWED_PREFIX: Final = "#/"

#: 单进程 worker 数。``serve`` 直接把它传给 uvicorn，不是一条建议：进程级
#: ConcurrencyLimiter、内存用量缓冲、SQLite 单写者全都依赖它。改成 2 会同时打破
#: 上游并发封顶、丢一半用量缓冲、并引入 ``database is locked``。
REQUIRED_WORKERS: Final = 1


# =============================================================================
# 10 · 数据目录与文件权限
# =============================================================================

DB_FILENAME: Final = "xingcha.db"
SECRET_FILENAME: Final = "secret.key"
BACKUP_DIRNAME: Final = "backups"

#: 容器内的运行 UID。固定值：宿主上的 bind mount 目录必须属于它，否则容器起来就是
#: Permission denied。Dockerfile 的 useradd、deploy.sh 的 chown 与报错文案都引用这
#: 一个值，免得改一处忘两处。
CONTAINER_UID: Final = 10001

#: 后台密码的最短长度。收进契约是因为它出现在四处（两个页面的校验与 ``minlength``、
#: CLI 提示），而"前端说 12、后端要 10"会让用户被一个说不清的错误挡住。
MIN_ADMIN_PASSWORD_LEN: Final = 12

#: 目录 0700、文件 0600。共享 VPS 上 0644 的库文件等于把 token hash 与 Fernet 密文
#: 交给任意本地账号。
DIR_MODE: Final = 0o700
FILE_MODE: Final = 0o600
UMASK: Final = 0o077

#: SQLite 必须跑在 WAL 上，启动时断言，否则拒绝启动：bind mount 落在网络盘上时 WAL
#: 会静默降级，症状是零星的 ``database is locked``。宁可起不来。
REQUIRED_JOURNAL_MODE: Final = "wal"

#: 上游 key 的来源优先级：DB 里的加密值优先，环境变量只在首次启动时一次性导入——
#: 环境变量会进 ``docker inspect`` 与 ``/proc/<pid>/environ``，不是长期存放处。
SETTING_KEY_OPENROUTER_API_KEY: Final = "openrouter.api_key"
SETTING_KEY_OPENROUTER_BASE_URL: Final = "openrouter.base_url"

#: 当前生效的上游来自哪个环境变量名。只用于展示，不参与解析：真正生效的 key 与
#: base_url 仍在上面那两个加密项里，切换只是把选中的那把复制进去，``load_upstream``
#: 一行都不用改。
SETTING_KEY_UPSTREAM_ACTIVE_ENV: Final = "upstream.active_env"

#: 用户手动添加的供应商列表（加密的 JSON 数组）。自动发现只看得到环境变量里的厂商
#: key，手填的中转也必须能留在切换列表里，否则切走就回不来。
#:
#: 存成一个 blob 而不是每家一行：``setting_svc`` 已对整个值加密，拆成多行要自己维护
#: 索引，而索引与内容不一致是最难查的一类状态。
SETTING_KEY_UPSTREAM_PROVIDERS: Final = "upstream.providers"

#: 手动添加的供应商名字长度上限。够写"公司内网中转"，短到能进表格一列。
PROVIDER_NAME_MAX: Final = 40

#: 可切换的上游：环境变量名 → 该厂商的 OpenAI 兼容 base_url。闭集，一处定义。
#:
#: 按变量名建表而不是猜 key 前缀：硅基流动、DeepSeek、Moonshot、Together、Fireworks、
#: Requesty 全都发 ``sk-`` 开头的 key，猜错的后果是把凭据发给错误的 base_url。
#: 而端点地址比变量名稳定得多——变量名各家文档天天变（``DEEPINFRA_API_KEY`` /
#: ``DEEPINFRA_TOKEN``、``AIMLAPI_`` / ``AIML_``），所以允许多个变量名指向同一个
#: base_url。表里没有的不会被扫出来（见 :func:`is_known_upstream_env`）——宁可少认，
#: 也不要把 ``GITHUB_TOKEN`` 当成模型 key 列进管理面。
#:
#: 只收 OpenAI 兼容的厂商。Anthropic / Gemini 原生 / Bedrock / Replicate 协议不同，
#: 填进来只会在第一次调用时以一个难懂的 4xx 失败；需要它们请走聚合方。
UPSTREAM_ENV_CANDIDATES: Final[dict[str, str]] = {
    # 聚合方（推荐：一把 key 打通几乎所有模型，也是星槎的默认形态）
    "OPENROUTER_API_KEY": "https://openrouter.ai/api/v1",
    "SILICONFLOW_API_KEY": "https://api.siliconflow.cn/v1",
    "SILICON_API_KEY": "https://api.siliconflow.cn/v1",  # 旧文档里的变体
    "TOGETHER_API_KEY": "https://api.together.xyz/v1",
    "TOGETHERAI_API_KEY": "https://api.together.xyz/v1",
    "FIREWORKS_API_KEY": "https://api.fireworks.ai/inference/v1",
    "DEEPINFRA_API_KEY": "https://api.deepinfra.com/v1/openai",
    "DEEPINFRA_TOKEN": "https://api.deepinfra.com/v1/openai",
    "REQUESTY_API_KEY": "https://router.requesty.ai/v1",
    "AIMLAPI_API_KEY": "https://api.aimlapi.com/v1",
    "AIML_API_KEY": "https://api.aimlapi.com/v1",
    "PORTKEY_API_KEY": "https://api.portkey.ai/v1",
    # 直连厂商
    "OPENAI_API_KEY": "https://api.openai.com/v1",
    "DEEPSEEK_API_KEY": "https://api.deepseek.com/v1",
    "MOONSHOT_API_KEY": "https://api.moonshot.cn/v1",
    "GROQ_API_KEY": "https://api.groq.com/openai/v1",
    "MISTRAL_API_KEY": "https://api.mistral.ai/v1",
    "XAI_API_KEY": "https://api.x.ai/v1",
    "PERPLEXITY_API_KEY": "https://api.perplexity.ai",
    "GEMINI_API_KEY": "https://generativelanguage.googleapis.com/v1beta/openai",
    "GOOGLE_API_KEY": "https://generativelanguage.googleapis.com/v1beta/openai",
    # 国内厂商。都实测过端点活着（无鉴权打 /models 或 /chat/completions 得 4xx）。
    "ZHIPUAI_API_KEY": "https://open.bigmodel.cn/api/paas/v4",
    "DASHSCOPE_API_KEY": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "ARK_API_KEY": "https://ark.cn-beijing.volces.com/api/v3",
    "MINIMAX_API_KEY": "https://api.minimax.chat/v1",
    "STEPFUN_API_KEY": "https://api.stepfun.com/v1",
    "BAICHUAN_API_KEY": "https://api.baichuan-ai.com/v1",
    # 其余聚合/推理方
    "NOVITA_API_KEY": "https://api.novita.ai/v3/openai",
    "CEREBRAS_API_KEY": "https://api.cerebras.ai/v1",
    "HYPERBOLIC_API_KEY": "https://api.hyperbolic.xyz/v1",
}

#: 这些上游没有 /models 端点（实测 404）。后果不是不能用，是目录为空 → 每条记录
#: ``cost_source=unknown``、判档一律回落 T2。管理面要说出来，否则用户以为星槎坏了。
UPSTREAM_ENV_WITHOUT_CATALOG: Final[frozenset[str]] = frozenset({"PERPLEXITY_API_KEY"})

#: 主题 cookie 的名字。用 cookie 而不是 localStorage：服务端渲染时就得知道选了哪个，
#: 否则 ``data-theme`` 要靠 JS 在首屏之后补，用户会看到一次闪白/闪黑；而内联 script
#: 被 CSP（``script-src 'self'``）挡着，加不进 <head>。
THEME_COOKIE: Final = "xc_theme"

#: 三态主题的闭集。``system`` 表示不写 ``data-theme``，交给 CSS 的
#: ``prefers-color-scheme`` 决定——这是默认，也是唯一"跟着系统变"的取值。
THEMES: Final = frozenset({"system", "light", "dark"})


#: 只属于编排层的 ``XINGCHA_*`` 变量名。
#:
#: 它们供 compose 插值端口/地址/挂载点，或供 ``deploy/linux/xc`` 选拓扑；应用本身不
#: 认识，而 ``env_file`` 会把整份 ``.env`` 注进容器。不登记的话
#: :func:`config.warn_unknown_env` 每次启动都会误报「拼错了？」，而人学会忽略这类
#: 警告之后，真的拼错时也不会有人看。
ORCHESTRATION_ENV_NAMES: Final = frozenset(
    {
        # 走不走共享网关。空 = 独立跑明文 HTTP；有值 = 那个 docker 网络的名字。
        # 它决定 deploy/linux/xc 要不要叠 docker-compose.gateway.yml——是**拓扑**开关。
        "XINGCHA_GATEWAY",
        # 派生值，用户不该设：deploy/linux/xc 从 XINGCHA_WEB_HOST 推出来后导出。
        # 登记在这里是因为万一有人手动设了，它会经 env_file 进容器——
        # 不登记就会被 warn_unknown_env 误报成"拼错了"。
        "XINGCHA_BIND_ADDR",
        "XINGCHA_WEB_HOST",
        # 独立跑时这个容器发布的宿主端口。走网关时端口是网关的（8443），
        # 由 deploy/linux/xc 导出成 XINGCHA_PUBLIC_PORT。
        "XINGCHA_WEB_PORT",
        "XINGCHA_PUBLIC_PORT",
        "XINGCHA_DATA_MOUNT",  # 宿主目录还是命名卷
    }
)


#: 星槎自己的默认上游变量名，优先于 ``UPSTREAM_ENV_CANDIDATES`` 里的任何厂商名。
#: 用通用名：上游可切换，名字里带 ``OPENROUTER`` 会在切到别家之后变成谎言。
ENV_DEFAULT_API_KEY: Final = "XINGCHA_API_KEY"
ENV_DEFAULT_BASE_URL: Final = "XINGCHA_BASE_URL"


#: 那一对默认变量的可接受写法。大小写不敏感，且永远认旧名——改配置项名是破坏性
#: 变更，而"升级对用户无感"是这个项目的头号承诺。
ENV_API_KEY_ALIASES: Final[tuple[str, ...]] = (
    ENV_DEFAULT_API_KEY,
    "XINGCHA_OPENROUTER_API_KEY",
)
ENV_BASE_URL_ALIASES: Final[tuple[str, ...]] = (
    ENV_DEFAULT_BASE_URL,
    "XINGCHA_OPENROUTER_BASE_URL",
)


def is_known_upstream_env(name: str) -> bool:
    """这个环境变量名是否是已知厂商的上游 key。

    大小写不敏感：``.env`` 里写小写是常见习惯，而 ``os.environ`` 在 Linux 上区分
    大小写，只认大写会让人以为功能坏了。
    """
    return name.upper() in UPSTREAM_ENV_CANDIDATES


def base_url_for_env(name: str) -> str | None:
    """已知厂商的 base_url；未知则 ``None``（由管理员在页面上填）。"""
    return UPSTREAM_ENV_CANDIDATES.get(name.upper())


def has_catalog(name: str) -> bool:
    """这个上游有没有 ``/models`` 端点。没有则目录为空，见上面的说明。"""
    return name.upper() not in UPSTREAM_ENV_WITHOUT_CATALOG


def vendor_label(name: str) -> str:
    """给管理面显示的厂商名：去掉 ``_API_KEY`` / ``_TOKEN`` 后缀。

    不另建"变量名 → 中文名"表——第二份映射要维护，而 ``DEEPSEEK`` 已经够清楚。
    """
    upper = name.upper()
    for suffix in ("_API_KEY", "_API_TOKEN", "_TOKEN", "_KEY"):
        if upper.endswith(suffix):
            return upper[: -len(suffix)]
    return upper


#: Langfuse 凭据。走加密存储而不是环境变量，理由同上游 key。
SETTING_KEY_TRACE_ENDPOINT: Final = "trace.endpoint"
SETTING_KEY_TRACE_PUBLIC_KEY: Final = "trace.public_key"
SETTING_KEY_TRACE_SECRET_KEY: Final = "trace.secret_key"

#: 上报的开关，与"地址配没配"分开：地址与凭据是配置，这一项是状态，停用不丢配置。
#: 合在一起的话"先停一下上报"就得清空地址、连带删掉两把 key，下次要重新找凭据贴一遍，
#: 而不好停的开关等于默认开着。判定是 ``endpoint and enabled``。
SETTING_KEY_TRACE_ENABLED: Final = "trace.enabled"

#: 上报目标列表（加密的 JSON 数组）与当前生效的那一个的名字。
#:
#: 上面四个键是前身，只存得下一份配置，而实际用法是"本机自建"与"云上"来回切，覆盖
#: 一次就丢了另一份的两把 key。迁移见
#: :func:`services.trace_targets.import_legacy_once`，一次性，做完删旧键。
#:
#: ``trace.active`` 是一个名字，空 = 全部停用。同一时刻只有一个生效不是产品取舍：
#: 追踪管道只有一条，装配的是一个 exporter。
SETTING_KEY_TRACE_TARGETS: Final = "trace.targets"
SETTING_KEY_TRACE_ACTIVE: Final = "trace.active"

#: 后台里声明过的 Agent 分组名，JSON 数组。分组本身只是 ``agent.group_name`` 上的
#: 字符串，这个键存的是还没有任何成员的分组——不存的话"新建分组"点完什么都不会发生，
#: 只能先建 Agent 再分组，而人的顺序通常是反的。
SETTING_KEY_AGENT_GROUPS: Final = "agent.groups"

#: 官方 OpenRouter 地址。中转时由管理员在设置里改写。
#:
#: ``OPENROUTER_BASE_URL`` 不被 pydantic-ai 读取（源码里只有 ``OPENROUTER_API_KEY`` /
#: ``_APP_URL`` / ``_APP_TITLE``），中转只能靠自建 ``AsyncOpenAI(base_url=...)`` 注入。
OPENROUTER_DEFAULT_BASE_URL: Final = "https://openrouter.ai/api/v1"


# =============================================================================
# 11 · 计量与计价
# =============================================================================


class CostSource(StrEnum):
    """费用数字的来源。四态从第一天就定死。

    只有 ``UPSTREAM`` 是上游报的真实费用，``CATALOG`` 与 ``GENAI_PRICES`` 都是估价。
    UI 与 CLI 必须区分显示——实测两者能差几百倍。

    pydantic-ai 自动填的 ``usage.cost`` 属于估价；上游 body 里真实的 ``cost`` 是
    float，被 ``isinstance(v, int)`` 过滤掉，所以 ``UPSTREAM`` 只能在 HTTP 层抓
    （见 ``core/costsink.py``）。
    """

    #: OpenRouter /v1/models 自带的价格。**主价源**——424/424 全有，抽样与 genai-prices 相等。
    CATALOG = "openrouter_catalog"
    #: genai-prices 估价。回落价源——实测只覆盖 66.7%，且在线更新补不上。
    GENAI_PRICES = "genai_prices"
    #: 上游自己在响应体 ``usage.cost`` 里报的费用。**唯一非预估的数字。**
    UPSTREAM = "upstream"
    #: 无法定价。此时 cost 为 null，与真实的 0 费用可区分。
    UNKNOWN = "unknown"


class Tier(StrEnum):
    """输出保证档位。四档从第一天就进 DB 的 CHECK 约束，现已全部实现
    （见 core/guarantee.AVAILABLE_TIERS）——CHECK 里不预留的话补档就是一次重建表。

    ``NONE`` 是第五个值：没有 schema 的纯文本 Agent 此前被报成 T3，而 T3 的含义是
    "schema 只进提示词、不做校验"，至少还有一份 schema。加一个值而不是把 ``tier``
    置空，是因为契约里它一直是必填字符串，改成可缺失会让所有调用方加一条判空。
    """

    T1 = "T1"  # 原生约束解码（strict=True 提交 schema）
    T2 = "T2"  # 校验后重试（默认档）
    T1P = "T1P"  # 两阶段：自由推理 → 格式化
    T3 = "T3"  # 仅提示词，不校验
    NONE = "none"  # 没有 schema，纯文本；不适用任何保证


class RunStatus(StrEnum):
    OK = "ok"
    SCHEMA_FAILED = "schema_failed"
    UPSTREAM_ERROR = "upstream_error"
    QUOTA = "quota"
    TIMEOUT = "timeout"
    CLIENT_ERROR = "client_error"


#: 判档只能看这个参数，不能看 ``response_format``：实测 OpenRouter 的 424 个模型里
#: 有 25 个只有后者，混用会把 T2 误判成 T1，于是对用户谎称"有原生保证"。
#: ``supported_parameters`` 为空 list 的语义是「未声明」而不是「全支持」，保守判 T2/T3。
CATALOG_NATIVE_SCHEMA_PARAM: Final = "structured_outputs"

#: 目录里代表「这个模型会推理」的参数名。两个都要认：实测 ``reasoning`` 与
#: ``include_reasoning`` 成对出现各 304 个，而 ``reasoning_effort`` 只有 165 个、是
#: 前者的子集，只认后者会把一半会推理的模型误判成不会。
CATALOG_REASONING_PARAMS: Final[frozenset[str]] = frozenset({"reasoning", "include_reasoning"})

#: 目录里代表「能调工具」的参数名。T2 的**工具通道**依赖它——模型不支持 tools 时，
#: 那条通道每次都 400（DeepSeek 思考模式就是这样），得改走提示词通道。
CATALOG_TOOLS_PARAM: Final = "tools"
