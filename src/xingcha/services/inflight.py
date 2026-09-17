"""正在跑的调用。进程内存里的一份在飞登记表。

**存在的理由是「现在能不能停」。** run 行只在调用结束时才落库（``RunTracker.submit``
是唯一写入点），``RunStatus`` 里也没有 ``running`` 这个取值——全是终态。所以一个正在
跑的调用在库里不存在任何一行，「此时此刻有没有干一半的任务」这个问题光查库永远答不
出来，只能在内存里自己记。

依赖单 worker（契约 §9 的 ``REQUIRED_WORKERS``）：多进程下每个进程只看得见自己那份，
页面上的数字会比真实情况少。和 ``services.ratelimit`` 是同一个前提。

**不加锁，也不能加。** 登记要从 ``RunTracker.__init__`` 这种同步代码里调得动，加锁就
得把它们改成 async。安全性来自别处：下面每个方法内部一个 ``await`` 都没有，asyncio
单线程下它们之间不会交错。
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass


@dataclass
class _Entry:
    """一条在飞记录。``kind`` 为 ``None`` 表示还没走到真正的模型调用。"""

    started: float
    token_name: str
    kind: str | None = None
    model: str | None = None
    run_id: str | None = None


@dataclass(frozen=True)
class InflightCall:
    """快照里的一条。**时长在取快照那一刻算好**——模板里算时长意味着每个模板都要
    自己拿一次 ``now``，而那几个 ``now`` 不是同一个时刻。
    """

    kind: str
    model: str
    token_name: str
    run_id: str | None
    elapsed_seconds: float


class InflightRegistry:
    """在飞登记表。

    生命周期必须挂在一个**有保证的** ``try/finally`` 上（``api.v1.authed_and_limited``），
    不能只挂在 ``RunTracker.submit`` 上：Agent 非流式路径的 submit 只出现在成功分支和
    ``fail()``（只接 ``XingchaError``）里，冒出个别的异常就漏注销一条。而漏注销的表现
    是页面上永远挂着一个并不存在的「正在跑」，于是管理员永远不敢升级——比没有这个功能
    更糟。
    """

    def __init__(self) -> None:
        self._entries: dict[int, _Entry] = {}
        self._tickets = itertools.count(1)

    def enter(self, *, token_name: str) -> int:
        """登记一次调用，返回注销用的号。调用方必须在 ``finally`` 里 :meth:`leave`。"""
        ticket = next(self._tickets)
        self._entries[ticket] = _Entry(started=time.monotonic(), token_name=token_name)
        return ticket

    def describe(self, ticket: int | None, *, kind: str, model: str, run_id: str) -> None:
        """补上「这一条到底在调什么」。号不认识就当没发生——注销先于补充是可能的
        （客户端在解析请求体期间断开），那不是错误。
        """
        if ticket is None:
            return
        entry = self._entries.get(ticket)
        if entry is not None:
            entry.kind, entry.model, entry.run_id = kind, model, run_id

    def leave(self, ticket: int | None) -> None:
        if ticket is not None:
            self._entries.pop(ticket, None)

    def snapshot(self) -> list[InflightCall]:
        """当前在飞的模型调用，先开始的在前。

        **只报补充过的那些。** ``/v1/models`` 这类目录读取也走同一个鉴权依赖，也会登记
        一条，但它根本不碰上游模型；把它算进来，一个每分钟轮询目录的客户端就能让页面
        常年显示「有调用在跑」，而这个面板唯一的用途是回答「现在停会不会腰斩谁」。
        还没补充的那些同理：它们连上游都还没发出去。
        """
        now = time.monotonic()
        rows = [
            InflightCall(
                kind=e.kind,
                model=e.model or "—",
                token_name=e.token_name,
                run_id=e.run_id,
                elapsed_seconds=now - e.started,
            )
            for e in self._entries.values()
            if e.kind is not None
        ]
        rows.sort(key=lambda r: r.elapsed_seconds, reverse=True)
        return rows
