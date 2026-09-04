"""Alembic 环境。

星槎不用 ``alembic.ini``：迁移在启动时以编程方式跑（见 ``db/migrate.py``），
配置由代码构造。这里只处理 Alembic 自己需要的上下文。
"""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import AsyncEngine

from xingcha.db.models import Base

target_metadata = Base.metadata


def _configure(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # SQLite 的 ALTER 能力有限，列变更必须走「建新表 → 拷数据 → 换名」。
        # 批处理模式让 Alembic 自动生成这套流程。
        render_as_batch=True,
        compare_type=True,
    )


def run_migrations_offline() -> None:
    context.configure(
        url=context.config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = context.config.attributes.get("connection", None)

    if isinstance(connectable, AsyncEngine):

        async def _run() -> None:
            async with connectable.connect() as conn:
                await conn.run_sync(lambda sync_conn: _do_run(sync_conn))

        asyncio.get_event_loop().run_until_complete(_run())
        return

    if connectable is None:
        from sqlalchemy import engine_from_config, pool

        connectable = engine_from_config(
            context.config.get_section(context.config.config_ini_section, {}),
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
        )
        with connectable.connect() as conn:
            _do_run(conn)
        return

    _do_run(connectable)


def _do_run(connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
