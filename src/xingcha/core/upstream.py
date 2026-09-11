"""上游 HTTP 客户端。

进程内**共享一个** ``httpx2.AsyncClient``：它自带连接池，每次请求新建一个等于每次
都重新握手 TLS，对一个跑在新加坡、被大陆客户端调用的服务来说这个开销很显眼。

三个必须显式设置的参数，每一个不设都会以难查的形式咬人：

``trust_env=True``
    读机器的 ``HTTP_PROXY`` / ``HTTPS_PROXY`` / ``NO_PROXY``。**这一项从 False 改了
    过来**，原先的理由是"代理不进代码，要走中转就配 ``openrouter.base_url``"。

    改的原因：有一类失败它挡不住——上游按**出口 IP 的区域**拒绝请求。实测 OpenRouter
    对 OpenAI 与 Google 的模型会回 ``This model is not available in your region.``，
    而机器上那个已经配好的代理正是唯一能用的出口，服务却绕过它直连。症状是"同一台
    机器上 curl 通、星槎 502"，而 502 的原因看起来完全在上游——最难查的一类。

    ``False`` 当初防的坑是真的：``ALL_PROXY=socks5://...`` 的机器上，客户端在**构造
    阶段**就抛 ``ImportError: socksio not installed``，服务直接起不来，报错还完全看
    不出跟代理有关。那个坑现在由 :func:`make_client` 的兜底接住——构造失败就退回不
    读环境并留一条 warning，而不是把整个服务拖死。

``max_retries=0``（openai SDK 侧）
    SDK 默认会重试 2 次。实测 timeout=0.3 时墙钟被放大到 2.17 秒，并且**把中转打了
    三遍**。重试策略应该只有一层，交给 pydantic-ai 的 retries / guarantee。

显式 ``Timeout``
    不设的话连接挂起会一直占着这个单进程的一个协程槽位。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx2

from .. import contract as C

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class UpstreamConfig:
    """上游连接参数。作为客户端缓存的 key——变了就整体重建。"""

    api_key: str
    base_url: str = C.OPENROUTER_DEFAULT_BASE_URL
    app_url: str | None = None
    app_title: str = "Xingcha"

    def normalized_base(self) -> str:
        return self.base_url.rstrip("/")


def attribution_headers(cfg: UpstreamConfig) -> dict[str, str]:
    """OpenRouter 的来源标注头。

    注意这些**必须手写**：``OpenRouterProvider`` 只在它自建 client 的分支里注入
    ``HTTP-Referer`` / ``X-Title``；一旦传了 ``openai_client=``（走中转时必须传），
    这段注入被整段跳过。所以下面这几行不是冗余代码，删掉会让 OpenRouter 后台看不到
    来源——**别当重复代码清理掉**。
    """
    h: dict[str, str] = {}
    if cfg.app_url:
        h["HTTP-Referer"] = cfg.app_url
    if cfg.app_title:
        h["X-Title"] = cfg.app_title
    return h


def new_async_client(**kwargs: Any) -> httpx2.AsyncClient:
    """所有出站 HTTP 客户端的**唯一**建法。读环境代理，socks 缺依赖时退回直连。

    **别在别处直接 new httpx2.AsyncClient。** 曾经有两处各建各的：这里改成了
    ``trust_env=True``，而 Agent 真正调模型的那条（``builder.make_provider``）还留着
    ``False``。症状极具迷惑性——模型目录拉得到、上游体检也通，**只有 Agent 调用**被
    上游按出口 IP 挡回来，而 502 的文案完全指向上游，一点看不出是自己没走代理。

    ``trust_env`` 的取舍见模块 docstring。ImportError 的兜底必须在**每一个**建客户端
    的地方都有，所以它只能在这一个函数里。
    """
    try:
        return httpx2.AsyncClient(trust_env=True, **kwargs)
    except ImportError:
        # socks 代理缺 socksio 就是在这里抛的。**必须兜住**：这一步失败等于服务起不来，
        # 而一台配了 socks 代理的机器本来是能直连上游的，不该因为读代理失败就整个不可用。
        log.warning(
            "读取环境代理失败（多半是 ALL_PROXY 指向 socks 但没装 socksio），"
            "本次退回不读环境代理直连上游。要走 socks 请装 socksio，"
            "或改用 http 代理，或配 openrouter.base_url 走中转。",
            exc_info=True,
        )
        return httpx2.AsyncClient(trust_env=False, **kwargs)


def make_client(cfg: UpstreamConfig, *, timeout: float) -> httpx2.AsyncClient:
    """建一个指向上游的客户端。

    不在这里塞 ``Authorization``：直通层与 Agent 层对鉴权头的处理不同（直通层要先
    剥掉调用方的头再换成上游 key），放在 client 默认头里反而容易搞混。
    """
    return new_async_client(
        base_url=cfg.normalized_base(),
        timeout=httpx2.Timeout(timeout, connect=min(15.0, timeout)),
        follow_redirects=False,
        limits=httpx2.Limits(max_connections=64, max_keepalive_connections=16),
    )


class UpstreamPool:
    """进程级客户端持有者。配置变化时整体重建。

    显式持有而不是模块级全局：管理员在设置页改了 key 或中转地址之后要能立刻生效，
    而一个藏在模块里的全局变量很难找到"该在哪儿失效它"。
    """

    def __init__(self, timeout: float) -> None:
        self._timeout = timeout
        self._cfg: UpstreamConfig | None = None
        self._client: httpx2.AsyncClient | None = None

    @property
    def configured(self) -> bool:
        return self._cfg is not None

    @property
    def config(self) -> UpstreamConfig | None:
        return self._cfg

    async def set_config(self, cfg: UpstreamConfig | None) -> None:
        """更新上游配置。相同配置是空操作，避免无谓地丢弃连接池。"""
        if cfg == self._cfg:
            return
        await self.aclose()
        self._cfg = cfg
        if cfg is not None:
            self._client = make_client(cfg, timeout=self._timeout)
            log.info("上游已切换到 %s", cfg.normalized_base())

    def client(self) -> httpx2.AsyncClient:
        if self._client is None or self._cfg is None:
            raise UpstreamNotConfigured
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class UpstreamNotConfigured(RuntimeError):
    """还没配上游 key。

    首次部署时会遇到，所以消息里要写清楚下一步做什么，而不只是说"没配置"。
    """

    def __init__(self) -> None:
        super().__init__(
            "还没有配置 OpenRouter API key。\n"
            "  管理后台的「设置」页填写——**当场生效**。\n"
            "  或命令行：xingcha config set openrouter.api_key -\n"
            "  （命令行写的值在启动时读取，写完要重启服务才生效）"
        )
