"""静态资源与模板的路径、版本号。

模板与静态文件放在 ``web/`` 下（不是 ``web/admin/`` 下）：它们随 wheel 分发，
路径写进了 ``pyproject.toml`` 的 ``artifacts``，挪动等于改打包清单。
"""

from __future__ import annotations

import hashlib
from functools import cache
from pathlib import Path
from typing import Any

from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

#: ``web/`` 包目录。模板与静态资源都在它下面。
WEB_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"


@cache
def _asset_digest(name: str, _stamp: tuple[int, int]) -> str:
    """静态资源内容的短哈希。

    ``_stamp`` 是 ``(mtime_ns, size)``，只用来做缓存键：**文件一改，键就变，
    哈希自动重算。** 不这么做的话进程内只算一次，而我们对静态资源发的是
    ``immutable`` 一年缓存——URL 不变 + 浏览器永久缓存 = 改了 CSS 却永远看不到，
    而且看起来像"改的地方没生效"。生产上无所谓（部署就是新进程），开发时能耗掉
    很长时间才想到是缓存。实际踩过。
    """
    return hashlib.sha256((STATIC_DIR / name).read_bytes()).hexdigest()[:8]


def asset(name: str) -> str:
    """静态资源的带版本 URL，形如 ``/admin/static/style.css?v=1a2b3c4d``。

    版本号是**文件内容的哈希**，不是应用版本号：开发期改一行 CSS 而版本号没变的
    情况太常见，而症状恰恰是最难认的那种——新模板配旧样式表，页面上的东西看起来
    "错位挤在一起"，而 HTML、CSS、Python 每一份单独看都是对的。

    有了内容哈希，URL 随内容变，于是可以放心声明 ``immutable`` 长缓存：
    平时零请求，升级后必取新的。这直接服务于"升级对用户透明"——用户不该需要知道
    "改完样式要硬刷新"这件事。

    每次渲染一个 ``stat()``（三个小文件），哈希本身由 :func:`_asset_digest` 缓存。
    """
    st = (STATIC_DIR / name).stat()
    return f"/admin/static/{name}?v={_asset_digest(name, (st.st_mtime_ns, st.st_size))}"


class VersionedStatic(StaticFiles):
    """静态文件 + 长缓存。

    URL 带内容哈希，所以 ``immutable`` 是**成立的**：同一个 URL 的内容永远不变。
    不加这个头的话浏览器只能按启发式缓存——既可能每次都 revalidate（白跑请求），
    也可能几天不问一次（升级后看到旧样式）。两种都不是我们想要的。
    """

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp
