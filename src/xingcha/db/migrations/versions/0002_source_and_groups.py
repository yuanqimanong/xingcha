"""0002 · 调用来源 + Agent 分组

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-09

**三个新列，全部可空、全部 ADD COLUMN**

SQLite 的 ``ALTER TABLE ... ADD COLUMN`` 只有在**可空或带常量默认值**时才是一次
元数据操作；加 NOT NULL 而无默认值要走「建新表 → 拷数据 → 换名」，在有真实数据的
线上库上就是一次停机迁移。这三列都可空，所以升级是瞬时的。

- ``run.client_ip`` / ``run.user_agent`` —— 谁在调。此前一行调用记录只知道用了
  哪把 token，不知道请求从哪来。key 泄漏时"它现在被谁在用"是第一个要回答的问题，
  而没有这两列就只能猜。历史行留 NULL，如实表示"那时候没记"。
- ``agent.group_name`` —— 分组。可空，NULL 表示默认分组；不给 server_default 是
  因为"没分过组"和"被明确放进一个叫默认的组"是两件事，而前者才是历史行的真相。

``downgrade()`` 必须可用：一个人在生产上跑迁移却没有已演练的回头路，是不可接受
的。SQLite 3.35+ 支持 ``DROP COLUMN``，alembic 的 batch 模式在更老的版本上会自动
退化成重建表。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("run", sa.Column("client_ip", sa.Text(), nullable=True))
    op.add_column("run", sa.Column("user_agent", sa.Text(), nullable=True))
    op.add_column("agent", sa.Column("group_name", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("agent") as b:
        b.drop_column("group_name")
    with op.batch_alter_table("run") as b:
        b.drop_column("user_agent")
        b.drop_column("client_ip")
