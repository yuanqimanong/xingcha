"""0003 · 试运行记录

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-09

**为什么不复用 run 表**

``run`` 是**账单与配额的事实来源**：总览的费用、成功率、配额结算全从它来。试运行
是管理员在后台按未保存的表单跑的一次实验，既不落进任何调用方的账、也不占配额。
混进去的后果是"这个月花了多少"里掺着调试开销，而那个数是要拿去对账的。

分表还有一个好处：试运行**存内容**（提示词与模型输出的完整链路），而 ``run`` 从来
不存内容——那是刻意的。两种保留期与两种隐私取舍，不该被同一张表的清理策略绑在一起。

**只留最近三次**

按 ``slug`` 保留最近 3 条，写入时顺手删旧的。不设保留期是因为这张表天然有界：
一个 Agent 最多 3 条，而 Agent 是个位数到几十。而它存着完整链路，无界增长就是
一个越来越大的、装着提示词原文的表。

``slug`` 允许为空串——新建页上的试运行还没有 slug，那几条归在一起。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_test_run",
        sa.Column("id", sa.Integer(), primary_key=True),
        #: 归属的 Agent。空串 = 新建页上跑的、还没有 slug。
        sa.Column("slug", sa.Text(), nullable=False, server_default=""),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("tier", sa.Text(), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("input", sa.Text(), nullable=False),
        sa.Column("output", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        #: 渲染好的消息链（JSON）。**这里面有提示词原文与模型输出。**
        sa.Column("chain_json", sa.Text(), nullable=True),
        sa.Column("elapsed_ms", sa.Integer(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("requests", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("violations", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("retries", sa.Integer(), nullable=False, server_default="0"),
        #: Decimal 的 str。NULL（查不到价）与真实的 0 必须可区分，与 run_usage 同口径。
        sa.Column("cost_usd", sa.Text(), nullable=True),
        sa.Column("cost_source", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
    )
    op.create_index("idx_agent_test_slug", "agent_test_run", ["slug", "created_at"])


def downgrade() -> None:
    op.drop_index("idx_agent_test_slug", table_name="agent_test_run")
    op.drop_table("agent_test_run")
