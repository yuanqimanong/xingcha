"""「正在跑」面板：登记不能漏，也不能多。

这个面板只有一个用途——回答「现在停会不会腰斩谁」。两个方向的错都会让它变成负资产：

* **漏注销**：页面上永远挂着一条并不存在的调用，于是管理员永远不敢升级。比没有这个
  面板更糟，因为它看起来是在正常工作。
* **多报**：``/v1/models`` 这类目录读取也走同一个鉴权依赖，把它算进来的话，一个每分钟
  轮询目录的客户端就能让页面常年显示「有调用在跑」。

所以重点在生命周期，不在渲染。静态检查 + 直接驱动那个依赖，不起服务、不碰网络。
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from xingcha.api import v1
from xingcha.foundation.errors import QuotaExceeded
from xingcha.services.auth import Principal
from xingcha.services.inflight import InflightRegistry
from xingcha.services.ratelimit import RateLimiter
from xingcha.web.admin import overview
from xingcha.web.admin.render import fmt_elapsed

SRC = Path(__file__).resolve().parents[1] / "src" / "xingcha"
PANEL = (SRC / "web" / "templates" / "_inflight.html").read_text(encoding="utf-8")

PRINCIPAL = Principal(user_id=1, token_id=1, kid="kid-test", token_name="测试令牌")


def fake_request(inflight: InflightRegistry, limiter: RateLimiter) -> Any:
    """依赖只碰 ``request.app.state.xc`` 和 ``request.state``，够用就行。"""
    xc = SimpleNamespace(inflight=inflight, limiter=limiter)
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(xc=xc)), state=SimpleNamespace()
    )


def describe_one(reg: InflightRegistry, ticket: int, model: str = "openai/gpt-4o") -> None:
    reg.describe(ticket, kind="agent", model=model, run_id="run-1")


# =============================================================================
# 登记表本身
# =============================================================================


def test_entry_is_invisible_until_described():
    """登记了但还没说在调什么 = 还没发出上游请求，不该算「正在跑」。"""
    reg = InflightRegistry()
    ticket = reg.enter(token_name="测试令牌")
    assert reg.snapshot() == []

    describe_one(reg, ticket)
    assert [c.model for c in reg.snapshot()] == ["openai/gpt-4o"]


def test_leave_removes_it():
    reg = InflightRegistry()
    ticket = reg.enter(token_name="测试令牌")
    describe_one(reg, ticket)
    reg.leave(ticket)
    assert reg.snapshot() == []


def test_describe_after_leave_is_a_noop():
    """客户端在解析请求体期间断开时，注销会先于补充发生。那不是错误。"""
    reg = InflightRegistry()
    ticket = reg.enter(token_name="测试令牌")
    reg.leave(ticket)
    describe_one(reg, ticket)
    assert reg.snapshot() == []


def test_unknown_or_missing_ticket_is_tolerated():
    reg = InflightRegistry()
    reg.leave(None)
    reg.leave(99999)
    reg.describe(None, kind="agent", model="m", run_id="r")
    reg.describe(99999, kind="agent", model="m", run_id="r")
    assert reg.snapshot() == []


def test_snapshot_puts_the_longest_running_first():
    """先看最久那条——它决定还要等多久才能停。"""
    reg = InflightRegistry()
    first = reg.enter(token_name="甲")
    second = reg.enter(token_name="乙")
    describe_one(reg, first, model="跑得久的")
    describe_one(reg, second, model="刚开始的")

    rows = reg.snapshot()
    assert [c.model for c in rows] == ["跑得久的", "刚开始的"]
    assert rows[0].elapsed_seconds >= rows[1].elapsed_seconds


# =============================================================================
# 生命周期挂在哪
# =============================================================================


async def test_dependency_registers_and_deregisters():
    reg = InflightRegistry()
    request = fake_request(reg, RateLimiter(per_minute=100, concurrent=10))

    gen = v1.authed_and_limited(request, PRINCIPAL)
    await gen.__anext__()
    describe_one(reg, request.state.inflight_ticket)
    assert len(reg.snapshot()) == 1

    with pytest.raises(StopAsyncIteration):
        await gen.__anext__()
    assert reg.snapshot() == []


async def test_dependency_deregisters_when_the_handler_blows_up():
    """**这条是整个功能的地基。**

    注销如果挂在 ``RunTracker.submit`` 上，Agent 非流式路径冒出一个非 ``XingchaError``
    的异常就会永远留一条，而那条幽灵会让管理员再也不敢重启。
    """
    reg = InflightRegistry()
    request = fake_request(reg, RateLimiter(per_minute=100, concurrent=10))

    gen = v1.authed_and_limited(request, PRINCIPAL)
    await gen.__anext__()
    describe_one(reg, request.state.inflight_ticket)

    with pytest.raises(RuntimeError):
        await gen.athrow(RuntimeError("处理器里炸了"))
    assert reg.snapshot() == [], "异常路径上漏了注销"


async def test_rate_limited_request_is_never_registered():
    """被限流拒掉的请求没在跑，不该出现在「正在跑」里。"""
    reg = InflightRegistry()
    request = fake_request(reg, RateLimiter(per_minute=0, concurrent=10))

    gen = v1.authed_and_limited(request, PRINCIPAL)
    with pytest.raises(QuotaExceeded):
        await gen.__anext__()
    assert reg.snapshot() == []


# =============================================================================
# 面板
# =============================================================================


def test_panel_keeps_polling_while_empty():
    """空态**不能**把整块渲染成空字符串。

    ``hx-swap="outerHTML"`` 换掉的是带 ``hx-trigger`` 的那个元素本身。空的时候不留下它，
    轮询器就跟着面板一起没了——之后再有调用进来，页面也不会自己回来，而这个面板的意义
    正是「不用手动刷新也能看见」。
    """
    tag = re.search(r"<div[^>]*hx-get=\"/admin/inflight\"[^>]*>", PANEL)
    assert tag, "_inflight.html 里找不到轮询用的元素"
    assert 'hx-swap="outerHTML"' in tag.group(0), f"轮询元素没有 outerHTML 换法：{tag.group(0)}"

    branch = PANEL.index("{% if inflight %}")
    assert tag.start() < branch, "轮询元素被挪进了 {% if inflight %} 里，空态会停止轮询"


def test_panel_route_exists():
    """模板轮询的那个地址得真的有人接。写死的字符串两边对不上时，表现是面板在第一次
    轮询后被一段 404 的 HTML 换掉——页面上直接看到一坨报错。
    """
    paths = {getattr(r, "path", None) for r in overview.router.routes}
    assert "/admin/inflight" in paths


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0.0, "0 秒"),
        (9.7, "9 秒"),
        (59.0, "59 秒"),
        (60.0, "1 分 00 秒"),
        (125.0, "2 分 05 秒"),
        (3600.0, "1 小时 00 分"),
        (3725.0, "1 小时 02 分"),
    ],
)
def test_fmt_elapsed(seconds: float, expected: str):
    """在飞时长按人读的方式给。``184000 ms`` 要人心算，而这个数字是用来做决定的。"""
    assert fmt_elapsed(seconds) == expected
