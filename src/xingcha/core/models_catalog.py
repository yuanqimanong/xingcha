"""上游模型目录。

两个用途：
1. ``GET /v1/models`` 里混入上游模型行
2. **价格**——这是 v1 的主价源

为什么 catalog 是主价源而不是 genai-prices：实测拿今天 OpenRouter 在售的 424 个模型
逐个跑 ``calc_price``，成功 283 个（66.7%），141 个抛 ``LookupError``；跑在线更新后
覆盖率**一个都没多**（上游数据持续滞后，不是本地快照过期），漏的全是新模型与
``:free`` / ``:batch`` 变体——恰好是最省钱那些。而 ``/v1/models`` 响应自带精确价格
且 424/424 全有，抽样与 genai-prices 完全相等。

**stale-while-error 是契约的一部分。** 拉取失败时返回上次成功的快照并标记 stale，
而不是报错或静默少返回：客户端会缓存这个列表并把 id 写进会话配置，一次上游抖动
如果让接口静默只返回 Agent 行，用户配置里的上游模型会被抹掉。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

import httpx2

from .. import contract as C

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ModelInfo:
    id: str
    name: str | None
    created: int | None
    #: ``supported_parameters``。判档只看这里的 ``structured_outputs``。
    supported: frozenset[str] = field(default_factory=frozenset)
    #: 每 token 单价（美元）。上游给的是字符串，保留 Decimal 精度。
    prompt_price: Decimal | None = None
    completion_price: Decimal | None = None
    cache_read_price: Decimal | None = None
    cache_write_price: Decimal | None = None

    @property
    def supports_native_schema(self) -> bool:
        """是否支持原生结构化输出。

        **只看 ``structured_outputs``，不看 ``response_format``。** 实测今天 424 个模型里
        后者 365 个、前者 340 个——有 25 个只有后者。混用会把 T2 误判成 T1，
        于是对用户谎称"有原生保证"。

        ``supported_parameters`` 为空 list 的模型语义是「未声明」而不是「全支持」，
        这里天然落到 False，是想要的保守判定。
        """
        return C.CATALOG_NATIVE_SCHEMA_PARAM in self.supported


def _dec(v: object) -> Decimal | None:
    if v is None:
        return None
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None
    # 上游对不可用的价格会给 "-1"
    return d if d >= 0 else None


def parse_models(payload: dict) -> dict[str, ModelInfo]:
    """把 ``/v1/models`` 的响应解析成目录。

    对缺字段一律宽容：上游随时可能加字段，而一个因为多了个键就崩掉的解析器会让
    整个服务在某天早上突然不可用。
    """
    out: dict[str, ModelInfo] = {}
    for m in payload.get("data") or []:
        mid = m.get("id")
        if not isinstance(mid, str) or not mid:
            continue
        pricing = m.get("pricing") or {}
        out[mid] = ModelInfo(
            id=mid,
            name=m.get("name"),
            created=m.get("created") if isinstance(m.get("created"), int) else None,
            supported=frozenset(m.get("supported_parameters") or []),
            prompt_price=_dec(pricing.get("prompt")),
            completion_price=_dec(pricing.get("completion")),
            cache_read_price=_dec(pricing.get("input_cache_read")),
            cache_write_price=_dec(pricing.get("input_cache_write")),
        )
    return out


class ModelsCatalog:
    """带 TTL 的目录缓存，失败时保留上次成功的快照。"""

    def __init__(self, *, ttl_seconds: int) -> None:
        self._ttl = ttl_seconds
        self._models: dict[str, ModelInfo] = {}
        self._fetched_at: float | None = None
        self._fetched_iso: str | None = None
        self._last_error: str | None = None

    # ------------------------------------------------------------- 状态
    @property
    def is_empty(self) -> bool:
        return not self._models

    @property
    def is_stale(self) -> bool:
        """快照是否已过期。**过期不等于不可用**——过期时仍然返回它，只是标记 stale。"""
        if self._fetched_at is None:
            return True
        return time.monotonic() - self._fetched_at > self._ttl

    @property
    def fetched_at(self) -> str | None:
        return self._fetched_iso

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def all(self) -> list[ModelInfo]:
        return list(self._models.values())

    def get(self, model_id: str) -> ModelInfo | None:
        return self._models.get(model_id)

    def supports_native_schema(self, model_id: str) -> bool:
        """未知模型保守判定为**不支持**。

        宁可降级到 T2 多花点重试成本，也不能对用户谎称有原生保证。
        """
        info = self._models.get(model_id)
        return bool(info and info.supports_native_schema)

    # ------------------------------------------------------------- 刷新
    async def refresh(self, client: httpx2.AsyncClient, api_key: str) -> bool:
        """拉一次目录。成功返回 True；失败返回 False 并**保留旧快照**。

        ``/v1/models`` 其实不需要 key（实测匿名可取），但带上它才能拿到与这把 key
        相关的可用性信息，而且行为与其它调用一致。
        """
        from datetime import UTC, datetime

        try:
            r = await client.get("/models", headers={"Authorization": f"Bearer {api_key}"})
            r.raise_for_status()
            parsed = parse_models(r.json())
        except Exception as e:
            self._last_error = f"{type(e).__name__}: {e}"
            log.warning("模型目录刷新失败（沿用上次快照）：%s", self._last_error)
            return False

        if not parsed:
            self._last_error = "上游返回了空目录"
            log.warning("模型目录刷新返回空，沿用上次快照")
            return False

        self._models = parsed
        self._fetched_at = time.monotonic()
        self._fetched_iso = datetime.now(UTC).isoformat(timespec="seconds")
        self._last_error = None
        log.info("模型目录已刷新：%d 个模型", len(parsed))
        return True

    async def ensure_fresh(self, client: httpx2.AsyncClient, api_key: str) -> None:
        """需要时刷新。刷新失败不抛——调用方按 stale 处理。"""
        if self.is_stale:
            await self.refresh(client, api_key)
