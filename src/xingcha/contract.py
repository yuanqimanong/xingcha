"""星槎对外契约的唯一定义处。

这个文件是整个项目的底座：所有正则、闭集、字段清单、类型纪律都以常量存在于此，
别处**只许引用、不许重新定义**（开发计划 §6 标准 2）。

依赖方向：本模块处在依赖图最底层，**不 import 任何 xingcha 模块**。

------------------------------------------------------------------------------
为什么这些东西必须冻结
------------------------------------------------------------------------------
上线后调用方手里只有三样东西：``base_url``、一把 ``sk-xc-`` key、一个 ``model``
字符串。凡是改动会打断这三样中任意一环的，都必须在第一次部署之前定死，此后
**只能加、不能改**。每个常量下面的 ``演进规则`` 注释说明允许怎么扩展。

``tests/test_contract_frozen.py`` 是这些常量的黄金测试：改动任一闭集都会让 CI 变红。
那不是测试坏了，是在提醒你正在做一次破坏性变更——要么换个设计，要么走 §3.12 的
契约号协商流程。
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
#: 这不是软件版本（那是 ``xingcha.__version__``）。软件可以天天发版，契约号只在
#: 发生**破坏性变更**时 +1——而破坏性变更本身应当几乎不发生。它存在的意义是：万一
#: 真的必须收紧某个行为（例如给直通路径加配额闸），有一条非硬切的发布通道，而不是
#: 让调用方某天突然收到 429。
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

#: 直通路径的配额执行**默认关闭**，由管理员显式打开。
#:
#: 契约 §3.9 把 ``PASSTHROUGH_ENFORCES_QUOTA`` 冻结成 False，演进规则写明"给直通层
#: 加配额闸是**收紧**，必须经协商入口发布"。所以这里的做法是能力做好、默认关，
#: 打开之后 ``/version`` 的 features 里会多一项 ``quota_passthrough``——
#: 那样它对既有调用方就不是一次静默的行为改变，而是部署者的显式决定。
FEATURE_QUOTA_PASSTHROUGH: Final = "quota_passthrough"


# =============================================================================
# 1 · 路径归属
# =============================================================================

#: 对外的四个前缀。除此之外不暴露任何路径。
PUBLIC_PREFIXES: Final[tuple[str, ...]] = ("/v1", "/api/v1", "/admin", "/healthz")

#: 星槎在 ``/v1`` 下自己实现的路径（相对 ``/v1/``，已归一化）。
#:
#: **这是一个闭集。** ``/v1`` 下其余一切路径全部字节级反代到上游，因此往这个集合里
#: 加一项 = 从反代手里"收回"一条路径 = 破坏性变更（调用方原本能用的上游端点突然
#: 变成星槎的语义）。
#:
#: 演进规则：新增星槎自有端点只能落在 ``/v1/xc/*``（见 RESERVED_V1_PREFIX）——该前缀
#: 从第一天起就永不反代，所以往里加东西不会从任何人手上拿走什么。
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

#: ``GET /v1/models/{id}`` 也是自有路径，但**只在 id 为单段时**。
#:
#: 为什么要这条：``/v1/models/{model}`` 是 OpenAI 标准的 retrieve-model，Cherry Studio
#: 与 Continue 一类客户端会用它验证模型是否存在。不把它列为自有路径就归反代——于是
#: 客户端拿 Agent slug 去问，请求打到 OpenRouter，拿回上游的 404，据此判定"这个模型
#: 不存在"。而按演进规则事后再从反代收回它算破坏性变更，等于**这个端点永久坏掉**。
#:
#: 为什么限定单段：Agent slug 永不含 ``/``（见 SLUG_RE），所以查 Agent 一定是单段。
#: 而上游 model id 一定含 ``/``（``vendor/name``），加上 OpenRouter 自己的
#: ``/v1/models/{author}/{slug}/endpoints``，多段的情形全部属于上游，留给反代才正确。
MODELS_ITEM_SEGMENTS: Final = 1

_MULTI_SLASH_RE: Final = re.compile(r"/+")


def normalize_v1_path(rel_path: str) -> str:
    """把 ``/v1/`` 之后的路径归一化成用于闭集匹配的形式。

    折叠重复斜杠、去掉首尾斜杠。**大小写敏感**（一律不折叠大小写）。

    没有这一步会有一个上线第一天就存在的静默 bug：``GET /v1/models/`` 带尾斜杠时，
    FastAPI 的 ``redirect_slashes`` 在 catch-all 路由存在的情况下**不生效**，请求
    直接落进 catch-all 被反代出去——客户端拿到 200、拿到 400 多个上游模型、
    **一个 Agent 都看不到，而且没有任何报错**。
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
#: 不这么做的话，浏览器客户端（Open WebUI、自建前端）直连星槎时，CORS 预检会由
#: OpenRouter 的策略决定，而星槎自己的响应又不带 CORS 头——表现为"非流式偶尔能用、
#: 浏览器直连必挂"。而等到要支持浏览器客户端时再拦截 OPTIONS，按演进规则算破坏性变更。
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
#: 三段各自的作用：
#:
#: - ``scheme`` —— 哈希算法分派位。换算法 = 新 scheme 数字，服务端**永久保留**全部
#:   历史 scheme 的校验分支，已签发的 key 不重签、不失效。
#: - ``kid`` —— **唯一查表键**，与 secret 无关、不可推导。这是整个设计的关键：
#:   如果拿"hash 本身"当查表键（很自然的做法），将来换成带盐的 argon2id 就无法反查，
#:   只能全表逐行 verify，``O(n)`` 次 argon2 每请求 = 送上门的 DoS。也就是说
#:   "已签发 key 永不失效"这个承诺会在迁移当天破掉。
#:   ``kid`` 同时让**对外显示的前缀不是活体秘密**——用"明文前 N 字符"当 prefix 的做法
#:   会把秘密本体的若干字符印在 UI、日志和 ``token list`` 里。
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

#: Agent slug。**禁含** ``/`` ``:`` ``.`` ``_`` 与大写字母。
#:
#: 演进规则：字符集只能**收缩到更严**，绝不可放宽——放宽会让原本返回 404 的字符串
#: 突然变成一个有效 Agent，那是行为的静默改变。
SLUG_RE: Final = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
SLUG_MIN_LEN: Final = 2
SLUG_MAX_LEN: Final = 48

#: 保留字：会与端点路径段或显式命名空间混淆的词。
SLUG_RESERVED: Final[frozenset[str]] = frozenset(
    {"models", "me", "health", "healthz", "readyz", "version", "xc", "admin", "api"}
)

#: 保留前缀：留给星槎将来可能内置的 Agent。
SLUG_RESERVED_PREFIX: Final = "xc-"

#: 上游裸模型 id。一定含 ``/``（``vendor/name``），可带 ``:free`` / ``:batch`` 变体后缀。
UPSTREAM_MODEL_RE: Final = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+(:[A-Za-z0-9._-]+)?$")

#: 星槎显式命名空间前缀。
#:
#: 为什么用 ``xc:`` 而不是 ``xc/``：冒号让显式命名空间在**结构上**不可能与上游的
#: ``vendor/model`` 混淆。``xc/agent/extract`` 长得就像一个上游 model id，只能靠
#: 规则顺序才不撞——能靠形状区分就不要靠顺序区分。
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

    **这是整个星槎唯一的路由分派点，也是最不能改的一条规则**——它编码在每一个
    调用方的 model 字符串里。

    按顺序三条，无例外：

    1. 以 ``xc:`` 开头 → 星槎显式命名空间（``xc:agent/<slug>`` 或 ``xc:model/<上游 id>``）
    2. 否则含 ``/`` → 上游裸模型 id，原样透传
    3. 其余 → Agent slug；查不到直接 404，**绝不猜测性地转发给上游**

    第 3 条的"绝不回落"很重要：如果查不到 Agent 就试着当上游模型转发出去，那么一个
    拼错的 slug 会静默变成一次真实的付费调用，而调用方以为自己在调 Agent。
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
            if not UPSTREAM_MODEL_RE.match(value):
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
#: 按 id 去重且 Agent 优先。
#:
#: **顺序必须冻结**：部分客户端取 ``data[0]`` 当默认模型，换排序即静默换模型。
MODELS_AGENTS_FIRST: Final = True

#: catalog 拉取失败或过期且刷新失败时的语义：返回上次成功的快照，并在 ``x_xingcha``
#: 里标 ``catalog_stale=true`` 与 ``fetched_at``。
#:
#: **降级语义必须冻结**，因为客户端会缓存这个列表并把 id 写进会话配置：
#: 一次上游抖动如果让接口只返回 Agent 行而不报错，用户配置里的上游模型会被
#: **静默抹掉**；如果返回 502，客户端可能整体判定端点不可用，连 Agent 也用不了。
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

#: 接受但**永久无语义**的字段。
#:
#: 元规则（这条本身也是契约）：**列入 ignore 的字段永久无语义，永不 honor。**
#: 需要新语义必须用新字段名，或走 ``x_xingcha`` 入参对象。
#:
#: 为什么：``user`` 正是 OpenAI 语义里天然的租户位，v2 做多用户/配额时一定会想拿它
#: 当 subject——而那一刻，所有在 v1 往 ``user`` 里塞了任意字符串的调用方，行为全部
#: 改变（突然被归到某个不存在的子账号、突然撞上别人的配额）。所以 ``user`` 在星槎里
#: **永久只作日志维度，租户归属永远只来自 token**。
REQUEST_IGNORE: Final[frozenset[str]] = frozenset({"user", "store", "metadata", "n"})

#: 直接 400 拒绝的字段。
#:
#: 演进规则：**reject 表只能缩小**，永不把字段从 honor/ignore 移进来（那是收紧）。
#: 把一个字段从 reject 移出去（开始支持它）是加法，允许。
#:
#: ``retries`` / ``max_retries`` / ``usage_limits`` 必须在这里：实测 ``run(retries=)``
#: 与 ``run(spec=)`` 都能覆盖 Agent 构造时的值，不拦住等于让调用方自行放大重试预算、
#: 绕过费用护栏。``response_format`` 也必须拦——输出形状由 Agent 定义决定，让调用方
#: 覆盖会让"200 即符合 schema"这个承诺失效。
#:
#: ``session_id`` 在 v1 就拒绝，而不是"先忽略、以后支持"：同一个请求在两个版本里
#: 两种语义（v1 无状态、v2 有状态）是无法回退的毁约。将来要支持就叫
#: ``x_xingcha.session_id``，或者把这个 400 放宽成 200（放宽是加法）。
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

#: 结构化输出的承载形式：``message.content`` **永远是字符串**
#: （``json.dumps(dict, ensure_ascii=False)``，不缩进）。调用方 ``json.loads`` 取回 dict。
#:
#: 永不改成把 dict 直接放进 content——那会让所有按 str 处理 content 的客户端崩掉。
#: 将来若要提供已解析形式，只能作为 ``x_xingcha.parsed`` **并行**提供，content 照旧。
CONTENT_ALWAYS_STR: Final = True

#: 金额的 JSON 类型：**字符串形式的 Decimal，或 null**。不是 number。
#:
#: 用 float 存不住 Decimal，而 ``null``（无法定价）与真实的 0 费用必须可区分——
#: 实测 OpenRouter 在售模型里约 1/3 在 genai-prices 查不到价。
COST_AS_STRING: Final = True

#: ``usage`` 的口径：**整轮累计**，包含全部 schema 重试与工具往返产生的 token 与费用。
#:
#: **这条必须冻结。** 一次 200 背后可能有 ``1 + retries`` 次模型调用（实测 retries=3
#: 时是 4 次）。等发现"一次调用怎么花了 4 倍"时，最自然的"修正"是只报最后一次尝试——
#: 那会让所有基于 usage 的账单核对、配额窗口聚合、成本看板**同时改变口径**，
#: 是无法回退的数值毁约。调用方要折算真实产出成本，用 ``x_xingcha.schema_retries``
#: 自行换算。
USAGE_IS_WHOLE_RUN: Final = True

#: 失败响应（429 / 422）也必须带 usage，否则失败 run 的花费不可见。
USAGE_ON_ERROR: Final = True

#: SSE 终止行。
SSE_DONE: Final = "data: [DONE]\n\n"

#: SSE 帧序列。**v0.2 的伪流式与 v0.4 的真流式逐字相同**——真流式上线时唯一的可观测
#: 变化是 ``content`` 帧变多了，而帧数变多对客户端是兼容的。这正是当初发伪流式而不是
#: 400 的理由：客户端会为一个 400 **写死绕过逻辑**（探测到就改走非流式），等真流式
#: 上线时反而打断它们。
#:
#: 中途失败的表达方式也在这里冻结：200 已经发出去之后无法改状态码，所以**不发
#: ``[DONE]``** 就是失败信号（OpenAI 自己也是这个行为）。调用方应当按"流是否以
#: ``[DONE]`` 结尾"判成败，而不是只看状态码。
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


#: 每个 error type 的 HTTP 状态码。**永不改动。**
#:
#: 两处刻意的拆分：
#:
#: - ``agent_spec_invalid`` (400) vs ``agent_build_failed`` (500) —— 用户填错和上游
#:   版本变动是两个完全不同的处置路径，一码两 HTTP 会让调用方无法分支。
#: - ``upstream_timeout`` (单次上游请求超时) vs ``request_timeout`` (整轮墙钟超时)
#:   —— ``Agent.run`` 没有 timeout 参数，per-Agent 超时走 ``model_settings['timeout']``，
#:   整轮墙钟只能靠 ``asyncio.timeout``，两者来源不同，排查路径也不同。
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

#: 5xx 对外只给固定文案 + run_id，细节只进日志。
#:
#: ``UserError`` / httpx / openai 的异常文本经常带完整 URL、偶尔带 header——直接回显
#: 就是一条上游 key 的泄漏路径。
INTERNAL_ERROR_MESSAGE: Final = "服务内部错误。请把 run_id 提供给管理员以便排查。"

#: 对外**不区分** token 无效 / 禁用 / 过期，一律 ``invalid_api_key``。
#:
#: 区分等于给公网一个 token 有效性 oracle（"这个 key 存在但过期了"是白送的信息）。
#: 区分只进日志。
AUTH_ERRORS_INDISTINGUISHABLE: Final = True


# =============================================================================
# 8 · 直通层的透明性与卫生
# =============================================================================

#: 转发给上游前必须**剥离**的请求头。
#:
#: 全部是客户端 IP 类的头。不剥掉的话真实来源 IP 就直接交给上游了——中转形同白建。
#: 注意这与"客户端 → 星槎"那一跳相反：那一跳恰恰**需要** XFF/X-Real-IP 才能记录
#: 真实来源，两处不能照抄同一条配置。
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

#: 回给客户端的上游响应头**白名单**。不在名单里的一律丢弃。
#:
#: 必须是白名单而不是黑名单：黑名单只剥 hop-by-hop 就逐字节透传的话，上游的
#: ``Set-Cookie`` 会落在你自己的域上，任何 echo/debug 头也一并出去。
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

#: 直通路径**从第一天就强制鉴权**：无有效 sk-xc- key 一律 401，绝不转发给上游。
#:
#: 这条配一条会红的测试。一个不鉴权的 catch-all 反代 + 一把付费 key = 开放代理，
#: 是本项目唯一的"一天烧光余额"级事故。
PASSTHROUGH_REQUIRES_AUTH: Final = True

#: v1 的直通路径记 run 行与 token，但**不执行配额**。
#:
#: 这一点必须写进 RUNBOOK，不能让人误以为 v1 有费用护栏。v1 唯一真正的钱刹车在
#: OpenRouter 侧——给服务端那把上游 key 单独设一个低额信用上限。
PASSTHROUGH_ENFORCES_QUOTA: Final = False


# =============================================================================
# 9 · 运行护栏
# =============================================================================

#: 请求体上限。超过即 413 ``request_too_large``。
#:
#: 直通层把 body 整块缓冲成 bytes（异步迭代器会强制 chunked，部分中转会拒），
#: 所以没有上限时一个大 POST 就能打死这个同时承载全部流量、SQLite 写入和用量缓冲的
#: 单进程。**这个值进契约**：事后调小是破坏性变更。
MAX_BODY_BYTES: Final = 8 * 1024 * 1024

#: 单个 JSON Schema 的上限（schema_guard）。
SCHEMA_MAX_BYTES: Final = 64 * 1024
SCHEMA_MAX_DEPTH: Final = 8
SCHEMA_MAX_PROPS: Final = 120
SCHEMA_MAX_ENUM: Final = 200

#: schema 里被拒绝的关键字。
#:
#: ``pattern`` / ``patternProperties`` 由 jsonschema 用 Python ``re`` 在事件循环上执行，
#: 且**每次 schema 重试都会重跑一遍**。一条 ``(a+)+$`` 就能把一核打满，整个单进程
#: 服务停摆。
SCHEMA_FORBIDDEN_KEYWORDS: Final[frozenset[str]] = frozenset({"pattern", "patternProperties"})

#: 只允许指向文档自身的 ``$ref``。
#:
#: jsonschema 在未给定封闭 registry 时会**真的去取**非本地 ``$ref``——
#: ``{"$ref": "http://attacker/x.json"}`` 是一个校验期 SSRF。除了这条前缀检查，
#: 构造 validator 时还必须传入**空的** ``referencing.Registry``，让远程取回在结构上
#: 不可能发生。
SCHEMA_REF_ALLOWED_PREFIX: Final = "#/"

#: 单进程 worker 数。**启动时断言，不是建议。**
#:
#: 进程级 ConcurrencyLimiter、内存用量缓冲、SQLite 单写者全都依赖它。任何人为了
#: "提高性能"改成 2，会同时静默打破上游并发封顶、丢一半用量缓冲、并引入
#: ``database is locked``——三个症状互不相关，排查成本极高。
REQUIRED_WORKERS: Final = 1


# =============================================================================
# 10 · 数据目录与文件权限
# =============================================================================

DB_FILENAME: Final = "xingcha.db"
SECRET_FILENAME: Final = "secret.key"
BACKUP_DIRNAME: Final = "backups"

#: 目录 0700、文件 0600。共享 VPS 上 0644 的库文件等于把 token hash 与 Fernet 密文
#: 交给任意本地账号。
DIR_MODE: Final = 0o700
FILE_MODE: Final = 0o600
UMASK: Final = 0o077

#: SQLite 必须跑在 WAL 上，启动时断言，否则**拒绝启动**。
#:
#: bind mount 落在网络盘或异常文件系统上时 WAL 会静默降级，症状是零星的
#: ``database is locked``——是最难查的一类问题。宁可起不来。
REQUIRED_JOURNAL_MODE: Final = "wal"

#: 上游 key 的来源优先级：DB 里的加密值优先，环境变量只在首次启动时一次性导入。
#:
#: 环境变量会进 ``docker inspect`` 与 ``/proc/<pid>/environ``，不是长期存放处。
SETTING_KEY_OPENROUTER_API_KEY: Final = "openrouter.api_key"
SETTING_KEY_OPENROUTER_BASE_URL: Final = "openrouter.base_url"

#: 官方 OpenRouter 地址。中转时由管理员在设置里改写。
#:
#: 注意：``OPENROUTER_BASE_URL`` 这个环境变量**不被 pydantic-ai 读取**（源码里只有
#: ``OPENROUTER_API_KEY`` / ``_APP_URL`` / ``_APP_TITLE``）。中转只能靠自建
#: ``AsyncOpenAI(base_url=...)`` 注入，见 core/builder.py。
OPENROUTER_DEFAULT_BASE_URL: Final = "https://openrouter.ai/api/v1"


# =============================================================================
# 11 · 计量与计价
# =============================================================================


class CostSource(StrEnum):
    """费用数字的来源。**四态从第一天就定死。**

    只有 ``UPSTREAM`` 是上游报的真实费用；``CATALOG`` 与 ``GENAI_PRICES`` 都是**估价**。
    UI 与 CLI 必须把两者区分显示，绝不把估价说成账单——实测两者能差几百倍。

    pydantic-ai 自动填的 ``usage.cost`` 属于估价（genai-prices），而上游 body 里真实的
    ``cost`` 因为是 float 被 ``isinstance(v, int)`` 过滤掉，哪儿都没留。所以 ``UPSTREAM``
    只能在 HTTP 层抓（见 ``core/costsink.py``）。
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
    """输出保证档位。**四档从第一天就进 DB 的 CHECK 约束**，v1 只实现 T2。

    后补 T1 / T1P 是纯加法；但如果 CHECK 里没有预留这两个值，补的时候就是一次
    需要重建表的迁移。
    """

    T1 = "T1"  # 原生约束解码（strict=True 提交 schema）
    T2 = "T2"  # 校验后重试（v1 唯一实现）
    T1P = "T1P"  # 两阶段：自由推理 → 格式化
    T3 = "T3"  # 仅提示词，不校验


class RunStatus(StrEnum):
    OK = "ok"
    SCHEMA_FAILED = "schema_failed"
    UPSTREAM_ERROR = "upstream_error"
    QUOTA = "quota"
    TIMEOUT = "timeout"
    CLIENT_ERROR = "client_error"


#: 判档只能看这个参数，**不能看 ``response_format``**。
#:
#: 实测今天 OpenRouter 的 424 个模型里 ``response_format`` 365 个、
#: ``structured_outputs`` 340 个——有 25 个只有前者。混用会把 T2 误判成 T1，
#: 于是对用户谎称"有原生保证"。
#:
#: 另：``supported_parameters`` 为空 list 的模型语义是「未声明」而不是「全支持」，
#: 必须保守判成 T2/T3。
CATALOG_NATIVE_SCHEMA_PARAM: Final = "structured_outputs"
