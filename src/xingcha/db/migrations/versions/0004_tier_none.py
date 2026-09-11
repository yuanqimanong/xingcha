"""0004 · 档位加一个 none（纯文本）

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-11

------------------------------------------------------------------------------
为什么要加这个值
------------------------------------------------------------------------------
没有配置 schema 的纯文本 Agent 此前被记成 ``T3``。T3 的含义是"schema 只进提示词、
不做校验"——那至少还有一份 schema；纯文本 Agent 一份都没有。

它是对外可见的：``x_xingcha.tier`` 直接把这个值给调用方，而调用方读它就是为了知道
"这次调用有没有结构保证、代价是什么"。对纯文本回答 T3，是个错误答案。

------------------------------------------------------------------------------
为什么这是一次真的迁移
------------------------------------------------------------------------------
``agent_version.tier`` 上有 CHECK 约束，四档写死在里面。不放开的话，保存一个纯文本
Agent 会在 INSERT 时被数据库拒绝——**而那是在保存的最后一步炸**，前面的校验全过了。

SQLite 不支持 ``ALTER TABLE ... DROP CONSTRAINT``，改 CHECK 只能重建表。alembic 的
``batch_alter_table`` 替我们做这件事：建新表、拷数据、换名字。

**这是加法**：新 CHECK 是旧 CHECK 的超集，已有的四档一行都不受影响。降级方向要先
把 ``none`` 归回 ``T3``，否则重建表时那些行会被新的（旧的）CHECK 拒绝——降级卡在
一条约束错误上，比不让降级更难收场。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_OLD = "tier IN ('T1','T2','T1P','T3')"
_NEW = "tier IN ('T1','T2','T1P','T3','none')"


def upgrade() -> None:
    with op.batch_alter_table(
        "agent_version",
        table_kwargs={"sqlite_autoincrement": False},
    ) as batch:
        batch.drop_constraint("ck_agent_version_tier", type_="check")
        batch.create_check_constraint("ck_agent_version_tier", _NEW)

    # 把历史上被误记成 T3 的纯文本 Agent 纠回来。
    #
    # 判据是 out_schema 为空——那才是"纯文本"的定义。真正选了 T3（有 schema、只进
    # 提示词）的 Agent out_schema 不为空，一行都不会被动到。
    op.execute(
        sa.text(
            "UPDATE agent_version SET tier = 'none' "
            "WHERE tier = 'T3' AND (out_schema IS NULL OR out_schema = '')"
        )
    )


def downgrade() -> None:
    # 先把值收回旧闭集，再收紧约束。顺序反了的话重建表会被 CHECK 拒绝。
    op.execute(sa.text("UPDATE agent_version SET tier = 'T3' WHERE tier = 'none'"))
    with op.batch_alter_table("agent_version") as batch:
        batch.drop_constraint("ck_agent_version_tier", type_="check")
        batch.create_check_constraint("ck_agent_version_tier", _OLD)
