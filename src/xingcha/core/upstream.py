"""上游 HTTP 客户端。

进程内**共享一个** ``httpx2.AsyncClient``：它自带连接池，每次请求新建一个等于每次
都重新握手 TLS，对一个跑在新加坡、被大陆客户端调用的服务来说这个开销很显眼。

三个必须显式设置的参数，每一个不设都会以难查的形式咬人：

``trust_env=False``
    httpx2 默认 ``True``，会读机器的 ``ALL_PROXY`` / ``HTTP_PROXY``。实测在
    ``ALL_PROXY=socks5://...`` 的机器上，客户端在**构造阶段**就抛
    ``ImportError: socksio not installed``——服务起不来，而报错完全看不出跟代理有关。
    星槎的定位就是"代理不进代码"，继承机器代理与这个定位直接冲突：要走中转请配
    ``openrouter.base_url``。

``max_retries=0``（openai SDK 侧）
    SDK 默认会重试 2 次。实测 timeout=0.3 时墙钟被放大到 2.17 秒，并且**把中转打了
    三遍**。重试策略应该只有一层，交给 pydantic-ai 的 retries / guarantee。

显式 ``Timeout``
    不设的话连接挂起会一直占着这个单进程的一个协程槽位。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

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


def make_client(cfg: UpstreamConfig, *, timeout: float) -> httpx2.AsyncClient:
    """建一个指向上游的客户端。

    不在这里塞 ``Authorization``：直通层与 Agent 层对鉴权头的处理不同（直通层要先
    剥掉调用方的头再换成上游 key），放在 client 默认头里反而容易搞混。
    """
    return httpx2.AsyncClient(
        base_url=cfg.normalized_base(),
        # 见模块 docstring：这是 E1，不可省
        trust_env=False,
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
            "  命令行：xingcha config set openrouter.api_key -\n"
            "  或在管理后台的「设置」页填写。"
        )
