"""用户手动添加的上游供应商。

------------------------------------------------------------------------------
为什么需要这一层
------------------------------------------------------------------------------

自动发现（:mod:`upstream_env`）只能看到**环境变量**里的厂商 key。而"我自己填的那个
中转地址"没有对应的环境变量，此前手填一次就直接覆盖了当前出口——**切走之后回不来**，
只能再手填一遍，把 key 再输一次。

所以手填的那些要落库，和自动发现的并列出现在同一个切换列表里。

------------------------------------------------------------------------------
形态
------------------------------------------------------------------------------

一个加密的 JSON 数组，存在单个 setting 里（``upstream.providers``）。
``setting_svc.set_`` 对整个值做加密，所以里面的 key 是加密落盘的。

存成一个 blob 而不是每家一行：一个 key、一次读写就够。拆成多行要自己维护索引，
而"索引里有、内容里没有"是最难查的一类状态。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from .. import contract as C
from ..foundation.crypto import Keyring
from . import setting as setting_svc

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Provider:
    """一个手动添加的上游。"""

    name: str
    base_url: str
    api_key: str

    @property
    def masked(self) -> str:
        return setting_svc.mask(self.api_key)


async def list_all(session: AsyncSession, keyring: Keyring) -> list[Provider]:
    """已保存的供应商。

    解不开或格式坏了时返回空表并告警，**不抛**：这一页的主要功能是"看当前出口 +
    切换"，为了一个坏掉的可选列表而让整页 500 是不成比例的。
    """
    raw = await setting_svc.get(session, keyring, C.SETTING_KEY_UPSTREAM_PROVIDERS)
    if not raw:
        return []
    try:
        items: Any = json.loads(raw)
        return [
            Provider(name=str(i["name"]), base_url=str(i["base_url"]), api_key=str(i["api_key"]))
            for i in items
            if isinstance(i, dict) and i.get("name") and i.get("base_url") and i.get("api_key")
        ]
    except (ValueError, TypeError, KeyError):
        log.warning(
            "已保存的供应商列表解析失败，按空表处理（%s）", C.SETTING_KEY_UPSTREAM_PROVIDERS
        )
        return []


async def _write(session: AsyncSession, keyring: Keyring, items: list[Provider]) -> None:
    await setting_svc.set_(
        session,
        keyring,
        C.SETTING_KEY_UPSTREAM_PROVIDERS,
        json.dumps(
            [{"name": p.name, "base_url": p.base_url, "api_key": p.api_key} for p in items],
            ensure_ascii=False,
        ),
    )


async def upsert(
    session: AsyncSession, keyring: Keyring, *, name: str, base_url: str, api_key: str
) -> Provider:
    """加一个，或按名字覆盖已有的那个。

    按**名字**去重（大小写不敏感）：同一个供应商填两遍是换 key 或改地址，
    而列表里出现两行同名条目之后没人分得清哪个在生效。
    """
    provider = Provider(name=name.strip(), base_url=base_url.strip(), api_key=api_key.strip())
    kept = [p for p in await list_all(session, keyring) if p.name.lower() != provider.name.lower()]
    await _write(session, keyring, [*kept, provider])
    return provider


async def get(session: AsyncSession, keyring: Keyring, name: str) -> Provider | None:
    for p in await list_all(session, keyring):
        if p.name.lower() == name.strip().lower():
            return p
    return None


async def remove(session: AsyncSession, keyring: Keyring, name: str) -> bool:
    items = await list_all(session, keyring)
    kept = [p for p in items if p.name.lower() != name.strip().lower()]
    if len(kept) == len(items):
        return False
    await _write(session, keyring, kept)
    return True
