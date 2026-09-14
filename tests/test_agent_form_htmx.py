"""模型报告那次 HTMX 请求必须把能力勾选一起带上。

回归测试。``_model_report.html`` 末尾用 ``hx-swap-oob`` **重渲染整个 #cap-list**，
而勾选状态只能由请求自己带回去（``agent_trial.agent_model_report`` 里的
``checked = {cap_*}``）。此前模板只写了 ``hx-include="#model"``，于是：

* ``checked`` 永远是空集；
* 触发器里又有 ``load``——打开一个已开能力的 Agent，页面加载完那一瞬勾就被冲掉；
* 用户没动过那一栏，点保存却把能力**静默清空**。

症状极具迷惑性：服务端渲染出来的 HTML 里 ``checked`` 是在的（不跑 JS 去抓页面只会
看到那一版），库里也是对的，只有真人在浏览器里看才是错的。

两半必须同时守：模板发不发，和路由收不收。只守一半的话，另一半被改坏时它还是绿的。
静态检查，不起服务、不碰网络。
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "xingcha"
FORM = (SRC / "web" / "templates" / "agent_form.html").read_text(encoding="utf-8")
REPORT = (SRC / "web" / "templates" / "_model_report.html").read_text(encoding="utf-8")
ROUTE = (SRC / "web" / "admin" / "agent_trial.py").read_text(encoding="utf-8")


def _model_report_tag() -> str:
    """取出 agent_form.html 里那个发 model-report 请求的标签。"""
    m = re.search(r"<div[^>]*hx-get=\"/admin/agents/model-report\"[^>]*>", FORM, re.S)
    assert m, "agent_form.html 里找不到发 /admin/agents/model-report 的元素"
    return m.group(0)


def test_hx_include_carries_the_capability_checkboxes() -> None:
    tag = _model_report_tag()
    include = re.search(r'hx-include="([^"]*)"', tag)
    assert include, f"那个元素没有 hx-include：{tag}"
    value = include.group(1)
    assert "#model" in value, "模型名还是要发的"
    # 光有 #model 不够——勾选状态发不上去，服务端就只能当成"一个都没勾"。
    assert "cap-list" in value, (
        f"hx-include 必须带上 #cap-list（当前是 {value!r}）。"
        "不带的话每次重渲染都会把已勾上的能力冲掉，保存后静默丢失。"
    )


def test_response_still_replaces_the_capability_list_out_of_band() -> None:
    """这条是上一条的前提：响应不再 OOB 换 #cap-list 的话，上面那条就没有意义了。"""
    assert 'id="cap-list"' in REPORT and "hx-swap-oob" in REPORT


def test_route_still_reads_checked_from_the_request() -> None:
    """路由这一半：勾选状态必须来自请求参数，不能凭空造。"""
    assert 'startswith("cap_")' in ROUTE, "agent_model_report 不再从 cap_* 读勾选状态了？"


def test_load_trigger_is_why_this_matters() -> None:
    """``load`` 触发器让这个 bug 在"打开页面"时就发生，而不是只在换模型时。

    留着这条不是为了拦 ``load``（它本身是对的：一进页面就该看到模型能干什么），
    而是把"为什么必须带 cap_*"钉在测试里——哪天有人把 hx-include 改回去，
    上面那条会红，而这条告诉他后果有多快。
    """
    assert "load" in _model_report_tag()
