"""管理后台。

一页一个模块，每个模块自带一个 ``router``；这里只负责按顺序接起来。拆开之前
这些全在一个 3000 行的 ``web/routes.py`` 里，而"上游"那一摊分居文件两处——
改一处忘另一处发生过不止一次。

三个跨页共用的模块：

* :mod:`.security` —— 会话、CSRF、同源、安全响应头；
* :mod:`.render` —— 模板渲染的唯一出口；
* :mod:`.runs` —— 调用记录的查询与聚合（总览 / 密钥详情 / 调用记录三页共用）。

对外只暴露 :func:`mount`，以及 :mod:`xingcha.app` 需要的 :class:`~.security.Denied`
与 :func:`~.security.security_headers`（它要给全局异常处理器用）。
"""

from __future__ import annotations

from fastapi import FastAPI

from . import (
    agent_trial,
    agents,
    guide,
    keys,
    login,
    logs,
    overview,
    quota,
    settings,
    upstreams,
)
from .assets import STATIC_DIR, VersionedStatic, asset
from .security import Denied, security_headers

__all__ = ["Denied", "asset", "mount", "security_headers"]

#: 装配顺序。**FastAPI 按注册顺序匹配路由，所以这个元组的顺序是有意义的。**
#:
#: ``agent_trial`` 必须排在 ``agents`` 前面：后者有 ``/agents/{slug}``，会把
#: ``/agents/model-report`` 这类固定路径吞掉。被吞掉是静默的——页面上只会出现
#: 一句"未知的 Agent：model-report"，看起来像数据问题。
#: ``tests/test_agent_form.py`` 里有一条用例专门盯着这个。
_PAGES = (login, overview, keys, logs, settings, upstreams, guide, agent_trial, agents, quota)


def mount(app: FastAPI) -> None:
    """挂载后台。静态文件内嵌进 wheel，不走 CDN——离线可用是硬约束。"""
    for page in _PAGES:
        app.include_router(page.router)
    app.mount("/admin/static", VersionedStatic(directory=str(STATIC_DIR)), name="xc-static")
