"""上报目标（Langfuse / OTel Collector / …）的列表。

------------------------------------------------------------------------------
为什么是列表
------------------------------------------------------------------------------

此前只有一份配置，改地址就是覆盖。而实际用法是"本机自建的那个 Langfuse"和
"云上那个"来回切——覆盖一次就把另一份的两把 key 弄丢了，切回去得重新去后台
翻凭据。和上游供应商是同一个问题、同一个解法（见 :mod:`providers`）。

------------------------------------------------------------------------------
同一时刻只有一个生效
------------------------------------------------------------------------------

不是产品取舍，是实现事实：追踪管道（``TracerProvider`` + ``BatchSpanProcessor``）
只有一条，装配的是**一个** exporter。给"两个都启用"编一套 UI，而底下只发一份，
是在页面上说谎。

所以 ``trace.active`` 存的是**一个名字**，空 = 全部停用。点另一条的「启用」就切
过去，旧的自动变停用——这一点必须写在页面上，否则用户会以为两条在并行上报。

------------------------------------------------------------------------------
形态
------------------------------------------------------------------------------

一个加密的 JSON 数组存在单个 setting 里（``trace.targets``）。里面有 secret key，
所以这一项**必须**在 ``setting_svc.SECRET_KEYS`` 里——漏了就是把明文凭据写进
SQLite，而页面上一切正常。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from .. import contract as C
from ..crypto import Keyring
from . import setting as setting_svc

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Target:
    """一个上报目标。

    ``public_key`` / ``secret_key`` 只有 Langfuse 需要（它用 Basic 认证）；
    OTel Collector 与 Jaeger 这类不校验身份的后端两个都留空。
    """

    name: str
    endpoint: str
    public_key: str = ""
    secret_key: str = ""

    @property
    def masked_secret(self) -> str:
        return setting_svc.mask(self.secret_key) if self.secret_key else ""


def _parse(raw: str | None) -> list[Target]:
    if not raw:
        return []
    try:
        items: Any = json.loads(raw)
        return [
            Target(
                name=str(i["name"]),
                endpoint=str(i["endpoint"]),
                public_key=str(i.get("public_key") or ""),
                secret_key=str(i.get("secret_key") or ""),
            )
            for i in items
            if isinstance(i, dict) and i.get("name") and i.get("endpoint")
        ]
    except (ValueError, TypeError, KeyError):
        log.warning("上报目标列表解析失败，按空表处理（%s）", C.SETTING_KEY_TRACE_TARGETS)
        return []


async def list_all(session: AsyncSession, keyring: Keyring) -> list[Target]:
    return _parse(await setting_svc.get(session, keyring, C.SETTING_KEY_TRACE_TARGETS))


async def _write(session: AsyncSession, keyring: Keyring, items: list[Target]) -> None:
    if not items:
        await setting_svc.unset(session, C.SETTING_KEY_TRACE_TARGETS)
        return
    await setting_svc.set_(
        session,
        keyring,
        C.SETTING_KEY_TRACE_TARGETS,
        json.dumps(
            [
                {
                    "name": t.name,
                    "endpoint": t.endpoint,
                    "public_key": t.public_key,
                    "secret_key": t.secret_key,
                }
                for t in items
            ],
            ensure_ascii=False,
        ),
    )


async def upsert(
    session: AsyncSession,
    keyring: Keyring,
    *,
    name: str,
    endpoint: str,
    public_key: str = "",
    secret_key: str = "",
) -> Target:
    """加一个，或按名字覆盖已有的那个。

    ``secret_key`` 留空时**沿用旧值**：页面上从不回显它，如果留空当成清空，那么
    "只想改一下地址"会把凭据静默抹掉，而表单上看不出任何异常。
    """
    name = name.strip()
    items = await list_all(session, keyring)
    old = next((t for t in items if t.name.lower() == name.lower()), None)
    target = Target(
        name=name,
        endpoint=endpoint.strip(),
        public_key=public_key.strip(),
        secret_key=secret_key.strip() or (old.secret_key if old else ""),
    )
    kept = [t for t in items if t.name.lower() != name.lower()]
    await _write(session, keyring, [*kept, target])
    return target


async def get(session: AsyncSession, keyring: Keyring, name: str) -> Target | None:
    for t in await list_all(session, keyring):
        if t.name.lower() == name.strip().lower():
            return t
    return None


async def remove(session: AsyncSession, keyring: Keyring, name: str) -> bool:
    """删一个。删掉的正好是当前生效的那个时，顺手停用。

    不停用的话 ``trace.active`` 会指向一个不存在的名字——``load_tracing`` 找不到、
    于是什么都不上报，而页面上「已启用」的徽章还亮着。
    """
    items = await list_all(session, keyring)
    kept = [t for t in items if t.name.lower() != name.strip().lower()]
    if len(kept) == len(items):
        return False
    await _write(session, keyring, kept)
    if (await active_name(session, keyring) or "").lower() == name.strip().lower():
        await setting_svc.unset(session, C.SETTING_KEY_TRACE_ACTIVE)
    return True


async def active_name(session: AsyncSession, keyring: Keyring) -> str | None:
    return await setting_svc.get(session, keyring, C.SETTING_KEY_TRACE_ACTIVE) or None


async def set_active(session: AsyncSession, keyring: Keyring, name: str | None) -> None:
    """启用某一个，或 ``None`` 全部停用。"""
    if name is None:
        await setting_svc.unset(session, C.SETTING_KEY_TRACE_ACTIVE)
    else:
        await setting_svc.set_(session, keyring, C.SETTING_KEY_TRACE_ACTIVE, name.strip())


async def active(session: AsyncSession, keyring: Keyring) -> Target | None:
    """当前生效的那个。名字指向不存在的条目时返回 ``None``。"""
    name = await active_name(session, keyring)
    return await get(session, keyring, name) if name else None


async def import_legacy_once(session: AsyncSession, keyring: Keyring) -> bool:
    """把旧的单份配置（``trace.endpoint`` 三兄弟）搬成列表里的一条。

    只在列表**为空**时做，做完删掉旧键——否则两处各有一份真相，改了一处另一处
    还在，而"我明明改了地址，发出去的还是旧的"是最难查的那种。
    """
    if await list_all(session, keyring):
        return False
    get_ = setting_svc.get
    endpoint = await get_(session, keyring, C.SETTING_KEY_TRACE_ENDPOINT)
    if not endpoint:
        return False

    await upsert(
        session,
        keyring,
        name="默认",
        endpoint=endpoint,
        public_key=await get_(session, keyring, C.SETTING_KEY_TRACE_PUBLIC_KEY) or "",
        secret_key=await get_(session, keyring, C.SETTING_KEY_TRACE_SECRET_KEY) or "",
    )
    # 旧的 trace.enabled 是 "0"/缺省，缺省按开算——与 load_tracing 的口径一致。
    enabled = await get_(session, keyring, C.SETTING_KEY_TRACE_ENABLED)
    await set_active(session, keyring, None if enabled == "0" else "默认")

    for key in (
        C.SETTING_KEY_TRACE_ENDPOINT,
        C.SETTING_KEY_TRACE_PUBLIC_KEY,
        C.SETTING_KEY_TRACE_SECRET_KEY,
        C.SETTING_KEY_TRACE_ENABLED,
    ):
        await setting_svc.unset(session, key)
    log.info("已把旧的单份上报配置迁成列表里的「默认」")
    return True
