"""表定义。

**这里的每一条约束都对应 docs/开发计划.md §3.10 的一条纪律。**

SQLite 的 ``ALTER TABLE`` 能力有限：事后给一列加 ``NOT NULL`` 或 ``UNIQUE`` 要走
「建新表 → 拷数据 → 换名」，在有真实数据的线上库上就是一次停机迁移。而升级档位是
「重启 + 自动迁移」，停机迁移直接违背它。所以下面这些必须在 0001 就写对：

- 所有主体表的 ``user_id NOT NULL``（v1 单用户，但 v2 加多用户不能停机）
- ``agent.slug`` 的 **UNIQUE**（slug 是全局命名空间，见契约 §3.3）
- ``token.hash_alg`` / ``kdf_params``（换哈希算法时盐与参数要随行走）
- ``agent_version.tier`` 的 CHECK 里**四档全列**，尽管 v1 只实现 T2
- ``run_usage.cost_usd`` 声明为 **TEXT**（存 Decimal 的 str；float 存不住，
  且 NULL「无法定价」必须与真实的 0 费用可区分）

时间一律存 **ISO-8601 UTC 字符串**。不用 DateTime 列类型：SQLite 无原生日期类型，
SQLAlchemy 的 SQLite 方言在取回时会丢掉 tzinfo，而配额窗口的口径必须是明确的 UTC。
存字符串让这件事在 schema 层面就是显式的。
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .. import contract as C


def utcnow() -> str:
    """当前时刻的 ISO-8601 UTC 字符串。全项目唯一的时间戳来源。"""
    return datetime.now(UTC).isoformat(timespec="microseconds")


class Base(DeclarativeBase):
    pass


# =============================================================================
# 系统配置
# =============================================================================


class Setting(Base):
    """键值配置。敏感值（OpenRouter key）Fernet 加密后存 ``value_enc``。

    上游 key 的**唯一**长期存放处。环境变量只在首次启动时一次性导入——环境变量会进
    ``docker inspect`` 与 ``/proc/<pid>/environ``。
    """

    __tablename__ = "setting"

    key: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    value_enc: Mapped[bytes | None] = mapped_column(sa.LargeBinary)
    is_secret: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    updated_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=utcnow)


# =============================================================================
# 用户与令牌
# =============================================================================


class User(Base):
    """v1 只有一行（``id=1``，由 0001 seed）。

    但表从第一天就存在且所有主体表都 ``NOT NULL`` 引用它——v2 加多用户时只需往这张表
    插行，不需要动任何既有表结构。
    """

    __tablename__ = "user"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    username: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    #: argon2id。v1 的 seed 行密码为空（未设置），首启向导里设定。
    password_hash: Mapped[str | None] = mapped_column(sa.Text)
    role: Mapped[str] = mapped_column(sa.Text, nullable=False, default="admin")
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=utcnow)

    __table_args__ = (sa.CheckConstraint("role IN ('admin','user')", name="ck_user_role"),)


class Token(Base):
    """API 令牌。**永不存明文**，明文只在签发时展示一次。

    ``kid`` 是唯一查表键，与 secret 无关、不可推导。为什么不用 hash 当查表键：
    换成带盐的 argon2id 之后就无法反查，只能全表逐行 verify，``O(n)`` 次 argon2
    每请求 = 送上门的 DoS。届时「已签发 key 永不失效」的承诺就破了。
    """

    __tablename__ = "token"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        sa.Integer, sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)

    #: 哈希方案编号，对应明文里的 ``<scheme>`` 段。服务端永久保留全部历史 scheme 的
    #: 校验分支。
    scheme: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=C.TOKEN_SCHEME_CURRENT)
    #: 唯一查表键。16 位小写字母数字。
    kid: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    #: 校验值。scheme=1 时是 ``sha256(secret)`` 的十六进制。
    hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    #: 算法名。带盐算法上线时新行写新值，旧行沿用旧算法直到用户主动轮换。
    hash_alg: Mapped[str] = mapped_column(
        sa.Text, nullable=False, default=C.TOKEN_SCHEME_1_ALG, server_default=C.TOKEN_SCHEME_1_ALG
    )
    #: KDF 参数（JSON）。scheme=1 为 NULL；argon2id 的盐与 t/m/p 必须随行走，
    #: 否则 scheme=2 上线时又要 ALTER TABLE。
    kdf_params: Mapped[str | None] = mapped_column(sa.Text)
    #: UI / 日志 / ``token list`` 里展示的标识。**不含秘密本体的任何字符。**
    display_prefix: Mapped[str] = mapped_column(sa.Text, nullable=False)

    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    expires_at: Mapped[str | None] = mapped_column(sa.Text)
    last_used_at: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=utcnow)

    __table_args__ = (sa.Index("idx_token_kid", "kid", unique=True),)


# =============================================================================
# Agent
# =============================================================================


class Agent(Base):
    """``slug`` 就是对外的 model id。

    ``slug`` 是 **全局** 唯一命名空间，不是 per-user。v2 加多用户时若改成 per-user，
    同一个 ``model="extract"`` 会随调用 token 的归属解析到不同 Agent——所有既有
    调用方的语义静默改变。per-user 命名空间只能通过新前缀 ``xc:u/<user>/<slug>``
    引入，裸 slug 的解析规则永不改变。
    """

    __tablename__ = "agent"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    current_version_id: Mapped[int | None] = mapped_column(sa.Integer)
    user_id: Mapped[int] = mapped_column(
        sa.Integer, sa.ForeignKey("user.id"), nullable=False, default=1
    )
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=utcnow)

    __table_args__ = (sa.Index("idx_agent_slug", "slug", unique=True),)


class AgentAlias(Base):
    """slug 改名的唯一出路。

    slug 发布后不可改名——调用方的代码里写着它。改名的正确做法是新建 Agent 并把旧
    slug 登记成别名，永久解析到新 Agent。这张表必须在 0001 就存在，否则第一次改名时
    就得加表 + 改解析逻辑，而那时候线上已经有调用方了。
    """

    __tablename__ = "agent_alias"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    alias: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    agent_id: Mapped[int] = mapped_column(
        sa.Integer, sa.ForeignKey("agent.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=utcnow)


class AgentVersion(Base):
    """``spec_json`` 整块存 AgentSpec dict，不拆列。

    拆列意味着 pydantic-ai 每次改字段都要迁移一次。整块存 + 写库前用官方 schema
    校验，把所有版本适配集中在 core/builder.py 一个文件里。

    注意 ``spec_json`` 必须是 ``model_dump(by_alias=True)`` 的结果：
    ``json_schema_path`` 字段的 alias 是 ``$schema`` 且未开 ``populate_by_name``，
    写全名会被静默丢弃，round-trip 会丢字段。
    """

    __tablename__ = "agent_version"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    agent_id: Mapped[int] = mapped_column(
        sa.Integer, sa.ForeignKey("agent.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    spec_json: Mapped[str] = mapped_column(sa.Text, nullable=False)

    #: 四档全列，尽管 v1 只实现 T2。没预留的话补 T1 时就是一次重建表的迁移。
    tier: Mapped[str] = mapped_column(sa.Text, nullable=False, default=C.Tier.T2.value)

    #: 输出 JSON Schema，NULL = 纯文本。
    #:
    #: 存的是 **$defs 内联展开后** 的 schema，不是用户提交的原文。否则 validator 用
    #: 带 $ref 的原文、模型收到展开版，两边不是同一份约束。
    out_schema: Mapped[str | None] = mapped_column(sa.Text)

    changelog: Mapped[str | None] = mapped_column(sa.Text)
    user_id: Mapped[int] = mapped_column(
        sa.Integer, sa.ForeignKey("user.id"), nullable=False, default=1
    )
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=utcnow)

    __table_args__ = (
        sa.UniqueConstraint("agent_id", "version", name="uq_agent_version"),
        sa.CheckConstraint(
            "tier IN ('T1','T2','T1P','T3')",
            name="ck_agent_version_tier",
        ),
    )


# =============================================================================
# 调用记录与计量
# =============================================================================


class Run(Base):
    """一次调用一行。Agent 路径与直通路径**共用**这张表。

    共用是有意的：两条路径花的是同一把上游 key 的钱，分表会让「这个月一共花了多少」
    变成一次 UNION，而那正是最常被问的问题。用 ``kind`` 区分。
    """

    __tablename__ = "run"

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)  # uuid4 hex
    #: ``agent`` | ``passthrough``
    kind: Mapped[str] = mapped_column(sa.Text, nullable=False)

    agent_id: Mapped[int | None] = mapped_column(sa.Integer)
    agent_version: Mapped[int | None] = mapped_column(sa.Integer)

    user_id: Mapped[int] = mapped_column(
        sa.Integer, sa.ForeignKey("user.id"), nullable=False, default=1
    )
    token_id: Mapped[int | None] = mapped_column(sa.Integer)
    session_id: Mapped[str | None] = mapped_column(sa.Text)

    #: 调用方实际传的 model 字符串（未解析前的原文），便于排查。
    model: Mapped[str] = mapped_column(sa.Text, nullable=False)
    tier: Mapped[str | None] = mapped_column(sa.Text)

    status: Mapped[str] = mapped_column(sa.Text, nullable=False)
    #: 对应 contract.ErrorType 的值。
    error_type: Mapped[str | None] = mapped_column(sa.Text)

    latency_ms: Mapped[int | None] = mapped_column(sa.Integer)
    started_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=utcnow)
    finished_at: Mapped[str | None] = mapped_column(sa.Text)

    __table_args__ = (
        sa.Index("idx_run_user_time", "user_id", "started_at"),
        sa.Index("idx_run_agent_time", "agent_id", "started_at"),
        sa.Index("idx_run_started", "started_at"),
        sa.CheckConstraint("kind IN ('agent','passthrough')", name="ck_run_kind"),
    )


class RunUsage(Base):
    """token 明细与费用。字段对齐 pydantic-ai 的 ``RunUsage``。

    **口径：整轮累计**，包含全部 schema 重试与工具往返产生的 token 与费用。
    一次 200 背后可能有 ``1 + retries`` 次模型调用。要折算真实产出成本，用
    ``schema_retries`` 自行换算——这个口径写进了契约，事后"修正"成只报最后一次
    会让所有历史账单数字漂移。
    """

    __tablename__ = "run_usage"

    run_id: Mapped[str] = mapped_column(
        sa.Text, sa.ForeignKey("run.id", ondelete="CASCADE"), primary_key=True
    )
    #: 实际执行的模型 id（Agent 路径下是 Agent 配置里的模型，不是请求里的 slug）。
    model: Mapped[str] = mapped_column(sa.Text, nullable=False)

    input_tokens: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    #: 包含式语义：cache_read 是 input 的子集，计价时按 cache 单价重算。
    #: 不做减法——genai-prices 与 RunUsage 的约定一致，自己减会高估 16%。
    cache_read_tokens: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    requests: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    tool_calls: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)

    #: schema 违规的**次数**（自己数）。
    schema_violations: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    #: 框架真实的重试序号（读 ``RunContext.retry``）。
    #:
    #: 两个必须分开：自己数的计数器在重试耗尽时会多计 1（预算耗尽后不再重试却仍计了
    #: 一次），正好在最需要精确告警的失败 run 上系统性偏移一格。
    schema_retries: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)

    #: **TEXT，存 Decimal 的 str。** NULL = 无法定价，与真实的 0 费用可区分。
    cost_usd: Mapped[str | None] = mapped_column(sa.Text)
    #: contract.CostSource 的值。四态从第一天就定死。
    cost_source: Mapped[str] = mapped_column(
        sa.Text, nullable=False, default=C.CostSource.UNKNOWN.value
    )

    #: 上游返回的额外计量维度（reasoning tokens、audio tokens 等）。
    #:
    #: 存 JSON 而不是加列：``RunUsage.__init__`` 接受任意 kwargs 并 setattr 成动态属性，
    #: provider 会借此塞新字段（实测 OpenRouter 会塞 ``output_reasoning_tokens``）。
    #: 上游每加一个维度就加一列的话，迁移会没完没了。
    extra_json: Mapped[str | None] = mapped_column(sa.Text)

    __table_args__ = (
        sa.CheckConstraint(
            "cost_source IN ('openrouter_catalog','genai_prices','upstream','unknown')",
            name="ck_run_usage_cost_source",
        ),
    )


# =============================================================================
# 配额（表在 0001 就建，执行逻辑在 v0.4）
# =============================================================================


class Quota(Base):
    """三级主体 × 三种窗口。

    表结构在 0001 就位，但 v1 **不执行**配额（契约 §3.9）。建表不花什么成本，而
    v0.4 加执行逻辑时不用再动 schema——这正是 expand-contract 想要的形状。

    窗口口径一律 **UTC**。用本地时区会让"今天"的边界随部署机时区变化，跨时区对账
    时对不上。
    """

    __tablename__ = "quota"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    subject_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    subject_id: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    window: Mapped[str] = mapped_column(sa.Text, nullable=False)
    limit_usd: Mapped[str | None] = mapped_column(sa.Text)  # Decimal 的 str
    limit_requests: Mapped[int | None] = mapped_column(sa.Integer)
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=utcnow)

    __table_args__ = (
        sa.UniqueConstraint("subject_type", "subject_id", "window", name="uq_quota_subject"),
        sa.CheckConstraint(
            "subject_type IN ('user','token','agent')", name="ck_quota_subject_type"
        ),
        sa.CheckConstraint("window IN ('day','month','total')", name="ck_quota_window"),
    )


# =============================================================================
# 管理后台会话
# =============================================================================


class WebSession(Base):
    """管理后台的登录会话。

    与 API token 完全分开：``sk-xc-`` 是给机器用的、走 Bearer 头；后台会话是给浏览器
    用的、走 SameSite=Strict 的 cookie。混用会让一个泄漏的 API key 直接拿到后台权限。
    """

    __tablename__ = "web_session"

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)  # 随机 token 的 sha256
    user_id: Mapped[int] = mapped_column(
        sa.Integer, sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )
    #: double-submit CSRF token 的比对值。
    csrf_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    expires_at: Mapped[str] = mapped_column(sa.Text, nullable=False)
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=utcnow)

    __table_args__ = (sa.Index("idx_web_session_expires", "expires_at"),)


#: 供迁移与测试引用的全部表名。
ALL_TABLES: tuple[str, ...] = tuple(Base.metadata.tables)
