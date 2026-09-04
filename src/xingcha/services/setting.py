"""配置项的读写。敏感值 Fernet 加密后落 ``setting`` 表。

这是 OpenRouter key 的**唯一长期存放处**。环境变量只在首次启动时一次性导入并告警——
环境变量会进 ``docker inspect`` 与 ``/proc/<pid>/environ``，不是长期存放处。
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import contract as C
from ..crypto import Keyring
from ..db.models import Setting, utcnow

log = logging.getLogger(__name__)

#: 哪些 key 是敏感的（加密存储、读取时默认脱敏展示）。
SECRET_KEYS: frozenset[str] = frozenset(
    {
        C.SETTING_KEY_OPENROUTER_API_KEY,
        C.SETTING_KEY_TRACE_SECRET_KEY,
    }
)

#: 允许通过 CLI / 管理面设置的 key 闭集。
#:
#: 用闭集而不是自由键值：一个拼错的 key 会静默存进去，然后你以为配好了、实际读的是
#: 默认值——这类问题排查起来非常费时。
KNOWN_KEYS: frozenset[str] = frozenset(
    {
        C.SETTING_KEY_OPENROUTER_API_KEY,
        C.SETTING_KEY_OPENROUTER_BASE_URL,
        C.SETTING_KEY_TRACE_ENDPOINT,
        C.SETTING_KEY_TRACE_PUBLIC_KEY,
        C.SETTING_KEY_TRACE_SECRET_KEY,
    }
)


class UnknownSettingKey(KeyError):
    pass


def _check_key(key: str) -> None:
    if key not in KNOWN_KEYS:
        raise UnknownSettingKey(f"未知的配置项 {key!r}。可用：{sorted(KNOWN_KEYS)}")


async def get(session: AsyncSession, keyring: Keyring, key: str) -> str | None:
    _check_key(key)
    row = (await session.execute(select(Setting).where(Setting.key == key))).scalar_one_or_none()
    if row is None or row.value_enc is None:
        return None
    return keyring.decrypt(row.value_enc)


async def set_(session: AsyncSession, keyring: Keyring, key: str, value: str) -> None:
    _check_key(key)
    row = (await session.execute(select(Setting).where(Setting.key == key))).scalar_one_or_none()
    enc = keyring.encrypt(value)
    is_secret = key in SECRET_KEYS
    if row is None:
        session.add(Setting(key=key, value_enc=enc, is_secret=is_secret, updated_at=utcnow()))
    else:
        row.value_enc = enc
        row.is_secret = is_secret
        row.updated_at = utcnow()


async def unset(session: AsyncSession, key: str) -> bool:
    _check_key(key)
    row = (await session.execute(select(Setting).where(Setting.key == key))).scalar_one_or_none()
    if row is None:
        return False
    await session.delete(row)
    return True


async def list_keys(session: AsyncSession) -> list[tuple[str, bool, str]]:
    """返回 ``(key, is_secret, updated_at)``。**不返回值本身。**"""
    rows = (await session.execute(select(Setting).order_by(Setting.key))).scalars().all()
    return [(r.key, r.is_secret, r.updated_at) for r in rows]


async def import_env_once(session: AsyncSession, keyring: Keyring, env_key: str | None) -> bool:
    """把环境变量里的上游 key 一次性导入 DB。

    **只在 DB 里还没有值时生效**，之后永久忽略。这样既让"首次启动填个环境变量就能跑"
    成立，又不会让环境变量长期成为事实上的配置源（那会让 Web 表单这条主线断掉，
    而且 key 会一直暴露在 ``docker inspect`` 里）。
    """
    if not env_key:
        return False
    existing = await get(session, keyring, C.SETTING_KEY_OPENROUTER_API_KEY)
    if existing:
        log.warning(
            "环境变量 XINGCHA_OPENROUTER_API_KEY 被忽略：数据库里已有上游 key。"
            "长期配置请在管理面修改，或用 `xingcha config set openrouter.api_key -`。"
        )
        return False
    await set_(session, keyring, C.SETTING_KEY_OPENROUTER_API_KEY, env_key)
    log.warning(
        "已把环境变量 XINGCHA_OPENROUTER_API_KEY 导入数据库并加密保存。"
        "建议从 .env 里删掉它——环境变量会出现在 docker inspect 与 /proc/<pid>/environ。"
    )
    return True


def mask(value: str) -> str:
    """展示用脱敏。保留足以辨认是哪一把 key 的信息，不泄漏可用部分。"""
    if len(value) <= 12:
        return "***"
    return f"{value[:8]}***{value[-4:]}"
