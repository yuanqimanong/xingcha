"""命令行的输出与错误边界。

三件事集中在这里：**怎么说**（带符号的三种消息）、**怎么对齐**（中文表格按显示
宽度而不是字符数）、**失败怎么收场**（运维类异常不甩 traceback）。
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import unicodedata

import typer

from ..bootstrap import prepare
from ..config import get_settings
from ..crypto import Keyring, KeyringInvalid, KeyringMissing
from ..db.engine import StartupRefused, make_engine, make_sessionmaker
from ..services import setting as setting_svc


def use_utf8_output() -> None:
    """把 stdout/stderr 换成 UTF-8。**在任何命令输出之前调用一次。**

    Windows 上不做这件事会真的崩：控制台默认代码页是 GBK(936)，而 ``✓`` ``✗``
    ``⚠`` 三个符号**都不在 GBK 里**。输出被重定向或管道接走时（``xingcha doctor
    > log.txt``、CI、docker logs），Python 用的是 locale 编码，于是 :func:`ok`
    第一次调用就 ``UnicodeEncodeError``——**整条命令带着 traceback 崩掉，只为了
    一个装饰字符**，而真正要说的那句话一个字都没印出来。

    ``deploy/windows/xc.bat`` 开头那句 ``chcp 65001`` 只覆盖它自己起的那个进程；
    用户在普通 PowerShell 里直接敲 ``xingcha`` 时没有它。

    ``errors="replace"`` 是最后一道保险：真遇到换不过来的终端，宁可印几个 ``?``，
    也不要因为一个符号丢掉整条消息。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        # 已经被接管成不可重配的流（测试里的 StringIO 之类）就跳过。不是错误：
        # 那种流本来就不经过控制台编码。
        with contextlib.suppress(ValueError, OSError):
            reconfigure(encoding="utf-8", errors="replace")


def err(msg: str) -> None:
    typer.secho(f"✗ {msg}", fg=typer.colors.RED, err=True)


def ok(msg: str) -> None:
    typer.secho(f"✓ {msg}", fg=typer.colors.GREEN)


def info(msg: str) -> None:
    typer.secho(f"→ {msg}", fg=typer.colors.CYAN)


def width(text: str) -> int:
    """字符串在终端里占几列。

    CJK 与全角标点占两列。用 ``len()`` 对齐中文表格会把列撑歪——而这个后台的
    用途名基本都是中文。
    """
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def pad(text: str, columns: int) -> str:
    """按显示宽度左对齐填充。超宽则截断并留一个空格分隔。"""
    w = width(text)
    if w >= columns:
        out = ""
        used = 0
        for ch in text:
            cw = width(ch)
            if used + cw > columns - 1:
                break
            out += ch
            used += cw
        return out + " " * (columns - used)
    return text + " " * (columns - w)


#: 这些是「运维需要动手处理」的失败，不是程序缺陷。
#:
#: 对它们甩 traceback 是没有意义的——栈帧不会告诉运维该做什么，而这些异常的消息里
#: 恰恰写着该做什么。所以在 CLI 边界上把它们转成干净的提示。
_OPERATIONAL_ERRORS = (KeyringMissing, KeyringInvalid, StartupRefused)


def run_async(coro):
    """跑一个异步命令，把运维类失败转成干净的退出。

    名字里的 ``_async`` 不是修饰，是**防撞**：命令体内部普遍有一个叫 ``run`` 的
    局部协程（``run_async(run())``）。这个函数要是也叫 ``run``，局部定义会把它遮掉，
    而症状是 ``RuntimeWarning: coroutine was never awaited`` 加一个空的 stdout——
    看起来像命令没跑，不像名字撞了。
    """
    try:
        return asyncio.run(coro)
    except _OPERATIONAL_ERRORS as e:
        err(str(e))
        raise typer.Exit(2) from e
    except setting_svc.UnknownSettingKey as e:
        err(str(e).strip("\"'"))
        raise typer.Exit(1) from e


def run_sync(fn, *args, **kwargs):
    """同上，用于同步命令。"""
    try:
        return fn(*args, **kwargs)
    except _OPERATIONAL_ERRORS as e:
        err(str(e))
        raise typer.Exit(2) from e


def bootstrap() -> tuple[object, object, Keyring]:
    """CLI 用的最小上下文：引擎 + 密钥环。不启动 web 层。

    走的是与 serve 完全相同的 ``bootstrap.prepare``——包括「密钥环缺失且库里已有
    密文时拒绝新建」那道守卫。CLI 自己再实现一遍的话，一次 ``config get`` 就能
    绕过它。
    """
    settings = get_settings()
    keyring = prepare(settings)
    engine = make_engine(settings.db_path)
    return engine, make_sessionmaker(engine), keyring
