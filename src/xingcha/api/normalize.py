"""路径归一化中间件。

**必须在路由之前生效。** 契约 §3.1 写着"匹配前先归一化"，但归一化函数放在
``contract.py`` 里是不够的——FastAPI 早在我们的代码跑起来之前就已经选好路由了。

不做这一步的后果是一个上线第一天就存在的静默 bug（已实测复现）：
``GET /v1/models/`` 带尾斜杠时，``redirect_slashes`` 在 catch-all 路由存在的情况下
**不生效**，请求直接落进 catch-all 被反代给上游——客户端拿到 200、拿到几百个上游
模型、**一个 Agent 都看不到，而且没有任何报错**。

用纯 ASGI 中间件而不是 ``BaseHTTPMiddleware``：后者会把请求体包一层，与直通层的
流式转发相互干扰，而这里要做的只是改一个字符串。
"""

from __future__ import annotations

from starlette.types import ASGIApp, Receive, Scope, Send

from .. import contract as C


class NormalizeV1Path:
    """把 ``/v1`` 下的路径折叠重复斜杠、去掉尾斜杠。

    只动 ``/v1``：``/admin`` 下的尾斜杠是正常的 Web 语义（``/admin/`` 就是首页），
    不该被一并改掉。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            path: str = scope.get("path", "")
            if path.startswith("/v1"):
                rel = C.normalize_v1_path(path[len("/v1") :])
                normalized = f"/v1/{rel}" if rel else "/v1"
                if normalized != path:
                    scope = dict(scope)
                    scope["path"] = normalized
                    # raw_path 是 starlette 路由的另一个来源，不同步会让两者打架
                    if scope.get("raw_path"):
                        scope["raw_path"] = normalized.encode("ascii", errors="ignore")
        await self.app(scope, receive, send)
