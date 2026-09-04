"""0001 · 初始 schema

Revision ID: 0001
Revises:
Create Date: 2026-09-03

------------------------------------------------------------------------------
这份迁移里的每条约束都是「事后补 = 停机」的那一类
------------------------------------------------------------------------------
SQLite 给已有列加 NOT NULL / UNIQUE 要走「建新表 → 拷数据 → 换名」。在有真实数据的
线上库上那就是一次停机迁移，直接违背「重启 + 自动迁移」的升级档位。所以下面这些必须
现在就对：

- 所有主体表 ``user_id NOT NULL`` + seed 一行 ``user(id=1)``
- ``agent.slug`` UNIQUE（slug 是全局命名空间）
- ``token.hash_alg`` / ``kdf_params``（换哈希算法时盐与参数随行走）
- ``agent_version.tier`` 的 CHECK 四档全列（v1 只实现 T2）
- ``run_usage.cost_usd`` 声明为 TEXT（Decimal 的 str；NULL ≠ 0）
- ``agent_alias`` 表（slug 改名的唯一出路）
- ``quota`` 表（v1 不执行，但表在位，v0.4 加逻辑时不动 schema）

``downgrade()`` 必须可用：一个人在生产上跑迁移却没有已演练的回头路，是不可接受的。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "setting",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value_enc", sa.LargeBinary(), nullable=True),
        sa.Column("is_secret", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("updated_at", sa.Text(), nullable=False),
    )

    op.create_table(
        "user",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.Text(), nullable=False, unique=True),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("role", sa.Text(), nullable=False, server_default="admin"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint("role IN ('admin','user')", name="ck_user_role"),
    )

    # v1 是单用户，但所有主体表都 NOT NULL 引用 user(id)。seed 这一行让 v2 加多用户
    # 变成纯插入，不需要动任何既有表结构。
    op.execute(
        sa.text(
            "INSERT INTO user (id, username, password_hash, role, is_active, created_at) "
            "VALUES (1, 'admin', NULL, 'admin', 1, :now)"
        ).bindparams(now=_now())
    )

    op.create_table(
        "token",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("scheme", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("kid", sa.Text(), nullable=False, unique=True),
        sa.Column("hash", sa.Text(), nullable=False),
        sa.Column("hash_alg", sa.Text(), nullable=False, server_default="sha256"),
        sa.Column("kdf_params", sa.Text(), nullable=True),
        sa.Column("display_prefix", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("expires_at", sa.Text(), nullable=True),
        sa.Column("last_used_at", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
    )
    op.create_index("idx_token_kid", "token", ["kid"], unique=True)

    op.create_table(
        "agent",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("slug", sa.Text(), nullable=False, unique=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("current_version_id", sa.Integer(), nullable=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False, server_default="1"
        ),
        sa.Column("created_at", sa.Text(), nullable=False),
    )
    op.create_index("idx_agent_slug", "agent", ["slug"], unique=True)

    op.create_table(
        "agent_alias",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("alias", sa.Text(), nullable=False, unique=True),
        sa.Column(
            "agent_id",
            sa.Integer(),
            sa.ForeignKey("agent.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", sa.Text(), nullable=False),
    )

    op.create_table(
        "agent_version",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "agent_id",
            sa.Integer(),
            sa.ForeignKey("agent.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("spec_json", sa.Text(), nullable=False),
        sa.Column("tier", sa.Text(), nullable=False, server_default="T2"),
        sa.Column("out_schema", sa.Text(), nullable=True),
        sa.Column("changelog", sa.Text(), nullable=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False, server_default="1"
        ),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("agent_id", "version", name="uq_agent_version"),
        # 四档全列。v1 只写 T2，但没预留的话补 T1 就是一次重建表。
        sa.CheckConstraint("tier IN ('T1','T2','T1P','T3')", name="ck_agent_version_tier"),
    )

    op.create_table(
        "run",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Integer(), nullable=True),
        sa.Column("agent_version", sa.Integer(), nullable=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False, server_default="1"
        ),
        sa.Column("token_id", sa.Integer(), nullable=True),
        sa.Column("session_id", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("tier", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error_type", sa.Text(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.Text(), nullable=False),
        sa.Column("finished_at", sa.Text(), nullable=True),
        sa.CheckConstraint("kind IN ('agent','passthrough')", name="ck_run_kind"),
    )
    op.create_index("idx_run_user_time", "run", ["user_id", "started_at"])
    op.create_index("idx_run_agent_time", "run", ["agent_id", "started_at"])
    op.create_index("idx_run_started", "run", ["started_at"])

    op.create_table(
        "run_usage",
        sa.Column(
            "run_id",
            sa.Text(),
            sa.ForeignKey("run.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("requests", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("tool_calls", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("schema_violations", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("schema_retries", sa.Integer(), nullable=False, server_default=sa.text("0")),
        # TEXT，不是 REAL：float 存不住 Decimal，且 NULL（无法定价）必须与真实的
        # 0 费用可区分——实测约 1/3 的在售模型在 genai-prices 查不到价。
        sa.Column("cost_usd", sa.Text(), nullable=True),
        sa.Column("cost_source", sa.Text(), nullable=False, server_default="unknown"),
        sa.Column("extra_json", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "cost_source IN ('openrouter_catalog','genai_prices','upstream','unknown')",
            name="ck_run_usage_cost_source",
        ),
    )

    op.create_table(
        "quota",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subject_type", sa.Text(), nullable=False),
        sa.Column("subject_id", sa.Integer(), nullable=False),
        sa.Column("window", sa.Text(), nullable=False),
        sa.Column("limit_usd", sa.Text(), nullable=True),
        sa.Column("limit_requests", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("subject_type", "subject_id", "window", name="uq_quota_subject"),
        sa.CheckConstraint(
            "subject_type IN ('user','token','agent')", name="ck_quota_subject_type"
        ),
        sa.CheckConstraint("window IN ('day','month','total')", name="ck_quota_window"),
    )

    op.create_table(
        "web_session",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("csrf_hash", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
    )
    op.create_index("idx_web_session_expires", "web_session", ["expires_at"])


def downgrade() -> None:
    # 顺序与建表相反：先删有外键指向别人的表。
    op.drop_index("idx_web_session_expires", table_name="web_session")
    op.drop_table("web_session")
    op.drop_table("quota")
    op.drop_table("run_usage")
    op.drop_index("idx_run_started", table_name="run")
    op.drop_index("idx_run_agent_time", table_name="run")
    op.drop_index("idx_run_user_time", table_name="run")
    op.drop_table("run")
    op.drop_table("agent_version")
    op.drop_table("agent_alias")
    op.drop_index("idx_agent_slug", table_name="agent")
    op.drop_table("agent")
    op.drop_index("idx_token_kid", table_name="token")
    op.drop_table("token")
    op.drop_table("user")
    op.drop_table("setting")


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat(timespec="microseconds")
