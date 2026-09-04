"""共享的启动序列。

``serve`` 与 CLI 子命令**都**从这里进入，而不是各写一份。

这不是为了少写几行：把「密钥环缺失时能不能新建」这条规则放两处，就会出现两处不一致
的实现——实际发生过一次，CLI 那份漏了守卫，于是密钥环丢失后跑一次 `xingcha config get`
就会静默重新生成，不但绕过单向门，还让服务重新"能启动"，而库里的密文已永久解不开。
一个概念只有一处定义（开发计划 §6 标准 2）。
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from .config import Settings
from .crypto import Keyring
from .db import migrate
from .db.engine import apply_umask

log = logging.getLogger(__name__)


def db_has_ciphertext(db_path: Path) -> bool:
    """库里是否已有加密数据。

    用同步 sqlite3 而不是 async engine：这个判断必须在**打开密钥环之前**完成，
    那时异步引擎还没建起来，而且这只是一次读。
    """
    if not db_path.exists():
        return False
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM setting WHERE is_secret = 1 AND value_enc IS NOT NULL"
            ).fetchone()
            return bool(row and row[0])
    except sqlite3.OperationalError:
        # setting 表还不存在（迁移之前）——那就肯定没有密文
        return False


def open_keyring(settings: Settings) -> Keyring:
    """打开密钥环，缺失且库里已有密文时**拒绝**新建。

    这是一扇单向门：静默重新生成会让 setting 表里的上游 key 永久解不开，而且当时
    不报任何错，等到下次真正调用上游时才表现为一个莫名其妙的失败。
    """
    has_ct = db_has_ciphertext(settings.db_path)
    return Keyring.load_or_create(settings.secret_path, allow_create=not has_ct)


def prepare(settings: Settings, *, migrate_db: bool = True) -> Keyring:
    """完整的启动前置序列，返回就绪的密钥环。

    顺序是有意的：

    1. umask —— 必须在任何文件被创建之前，否则先建出来的文件权限就宽了
    2. 数据目录 —— 建目录并收紧权限
    3. 迁移（内含备份） —— 失败即抛，绝不带着半旧 schema 继续
    4. 密钥环 —— 见 :func:`open_keyring`
    """
    apply_umask()
    settings.ensure_data_dir()
    if migrate_db:
        before, after = migrate.upgrade_to_head(settings.db_path, settings.backup_dir)
        if before != after:
            log.info("数据库已从 %s 升级到 %s", before or "(空库)", after)
    return open_keyring(settings)
