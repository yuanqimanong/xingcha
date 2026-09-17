"""Windows 拆连接时那条假 ERROR 要被吞掉，别的一条都不许吞。

这个过滤器的风险全在「吞多了」那一侧：它挂在 loop 的异常 handler 上，是进程里最后一道
报障途径，放宽一点就会把真事故变成静默。所以这里的重点不是「那条噪音没了」，而是**边界**
——同类型但来源不同、同来源但类型不同、以及链下去的那一步，一条都不能少。

不依赖真的 Windows：被测函数认的是 traceback 里的帧名 + 文件名，用 ``compile`` 就能造出
同形的一帧，于是 Linux 的 CI 上也跑得出来。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from xingcha.foundation.errors import (
    install_loop_noise_filter,
    is_proactor_teardown_reset,
)

#: stdlib 那一帧的 co_filename 长什么样。真机上是绝对路径，被测函数只看结尾。
STDLIB_FILE = str(Path("asyncio") / "proactor_events.py")

#: 造假帧用的模板。``%s`` 是函数名——帧名只能由 compile 决定，伪造不了别的路子。
_RAISER = "def %s(exc):\n    raise exc\n"


def raised_in(frame_name: str, filename: str, exc: BaseException) -> BaseException:
    """造一条 traceback 顶着指定帧名 / 文件名的异常。

    直接 ``ConnectionResetError(...)`` 是不够的：那条异常的 ``__traceback__`` 是 ``None``，
    而被测函数认的正是 traceback。帧名与 ``co_filename`` 只能由 ``compile`` 决定，所以这里
    真的编译一个函数再真的抛一次。
    """
    ns: dict[str, Any] = {}
    exec(compile(_RAISER % frame_name, filename, "exec"), ns)
    try:
        ns[frame_name](exc)
    except BaseException as caught:
        return caught
    raise AssertionError("没抛出来")


def teardown_noise() -> BaseException:
    """真机上那条：stdlib 的帧 + WinError 10054。"""
    return raised_in("_call_connection_lost", STDLIB_FILE, ConnectionResetError(10054, "forced"))


def test_recognises_the_stdlib_teardown_reset():
    assert is_proactor_teardown_reset(teardown_noise())


def test_recognises_the_aborted_variant():
    """10053 从同一帧抛出来同样是假的——同一句 shutdown()，只是对端断法不同。"""
    exc = raised_in("_call_connection_lost", STDLIB_FILE, ConnectionAbortedError(10053, "forced"))
    assert is_proactor_teardown_reset(exc)


def test_same_error_from_our_own_code_is_not_noise():
    """上游传输中途断开也是 ConnectionResetError。这条必须照常报出来。

    只按异常类型判的实现会在这里翻车，而翻车的后果是「上游断流」变成一条都不打——
    那正是最需要日志的时候。
    """
    exc = raised_in("read_body", "xingcha/api/passthrough.py", ConnectionResetError(10054, "真断"))
    assert not is_proactor_teardown_reset(exc)


def test_other_errors_from_that_frame_are_not_noise():
    """帧对得上也不够：认的是「那一句 shutdown 抛的连接错」，不是「那个函数里的一切」。"""
    exc = raised_in("_call_connection_lost", STDLIB_FILE, ValueError("别的毛病"))
    assert not is_proactor_teardown_reset(exc)


def test_no_exception_in_context_is_not_noise():
    """``call_exception_handler`` 的 context 里不一定有 exception（比如 Task 被回收的告警）。"""
    assert not is_proactor_teardown_reset(None)


async def test_handler_swallows_only_the_noise_and_chains_the_rest():
    loop = asyncio.get_running_loop()
    seen: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, ctx: seen.append(ctx))

    restore = install_loop_noise_filter(loop)
    try:
        loop.call_exception_handler({"message": "噪音", "exception": teardown_noise()})
        assert seen == [], "假报错没被吞"

        real = raised_in("anything", "elsewhere.py", ConnectionResetError(10054, "真断"))
        loop.call_exception_handler({"message": "真事故", "exception": real})
        loop.call_exception_handler({"message": "连 exception 都没有"})
        assert [c["message"] for c in seen] == ["真事故", "连 exception 都没有"]
    finally:
        restore()


async def test_restore_puts_the_previous_handler_back():
    """``--reload`` 下同一进程反复起停，不还原就会一层层套上去。"""
    loop = asyncio.get_running_loop()

    def original(_loop: Any, _ctx: Any) -> None: ...

    loop.set_exception_handler(original)
    restore = install_loop_noise_filter(loop)
    assert loop.get_exception_handler() is not original
    restore()
    assert loop.get_exception_handler() is original
