"""SQLite 引擎、PRAGMA 与启动断言。

一条断言在这里，是**拒绝启动**而不是警告：WAL 必须真的生效。bind mount 落在网络盘
或异常文件系统上时 WAL 会静默降级，症状是零星的 ``database is locked``——最难查的
一类问题。宁可起不来。

单 worker 是同一级别的硬约束，但不靠运行期断言：:func:`cli.serve` 直接把
``C.REQUIRED_WORKERS`` 传给 uvicorn，没有可配之处。
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .. import contract as C
from ..config import StartupRefused

log = logging.getLogger(__name__)


@contextmanager
def sqlite_conn(target: str | Path, *, uri: bool = False) -> Iterator[sqlite3.Connection]:
    """一次同步 sqlite3 连接，**用完真的关掉**。

    存在的理由是 ``sqlite3`` 的一个反直觉设计：

        with sqlite3.connect(p) as conn:   # ← 这句**不关连接**

    ``Connection.__exit__`` 只提交或回滚事务，连接本身留着，等 GC。POSIX 上看不出
    毛病（打开的文件照样能 unlink），于是这个泄漏可以活很久；Windows 上立刻变成
    ``WinError 32：另一个程序正在使用此文件``——备份演练里 ``rmtree(data/)`` 直接
    失败，而报错完全指不到"上一次 backup() 没关连接"。

    同步 sqlite3 用在异步引擎起来之前或之外的几处：启动前探密文、备份、校验、恢复。
    这些都在关键路径上（每次启动都跑），一次漏一个 fd 不是可以忽略的量级。

    只关不提交：这里的用法要么是只读，要么是自带事务语义的 ``VACUUM INTO``。
    需要提交的调用方自己 ``conn.commit()``。
    """
    conn = sqlite3.connect(target if uri else str(target), uri=uri)
    try:
        yield conn
    finally:
        conn.close()


# 定义在 config.py（比这一层更早、且只依赖 contract）。这里导出，保持既有 import 可用。
__all__ = ["StartupRefused"]


def make_engine(db_path: Path, *, echo: bool = False) -> AsyncEngine:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        echo=echo,
        # 单进程单 worker，连接池保持小而稳
        pool_size=5,
        max_overflow=5,
        pool_pre_ping=True,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _pragmas(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        # 读写不互斥。用量批量写入与请求路径的读同时发生，没有 WAL 会互相阻塞。
        cur.execute("PRAGMA journal_mode=WAL")
        # WAL 下 NORMAL 是安全的：崩溃最多丢最近一次 checkpoint 之后的事务，
        # 不会损坏数据库。FULL 会让每次提交都 fsync，写入延迟进入请求路径。
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        # 写锁竞争时等待而不是立刻抛 database is locked
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

    return engine


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


async def assert_wal(engine: AsyncEngine) -> None:
    """确认 WAL 真的生效，否则拒绝启动。

    只设 PRAGMA 不验证是不够的：SQLite 在不支持的文件系统上会**静默**回落到
    journal 模式，什么都不报。
    """
    async with engine.connect() as conn:
        mode = (await conn.execute(text("PRAGMA journal_mode"))).scalar_one()
    if str(mode).lower() != C.REQUIRED_JOURNAL_MODE:
        raise StartupRefused(
            f"SQLite 的 journal_mode 是 {mode!r}，而星槎要求 {C.REQUIRED_JOURNAL_MODE!r}。\n"
            "通常意味着数据目录落在了网络存储或不支持 WAL 的文件系统上。\n"
            "把 data/ 换到宿主本地磁盘（ext4/xfs）再启动。"
        )
    log.debug("journal_mode = %s", mode)


def apply_umask() -> None:
    """收紧本进程创建文件的默认权限。

    共享 VPS 上 0644 的库文件等于把 token hash 与 Fernet 密文交给任意本地账号。
    """
    os.umask(C.UMASK)


@asynccontextmanager
async def session_scope(
    maker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """一个事务作用域。异常时回滚。"""
    async with maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
