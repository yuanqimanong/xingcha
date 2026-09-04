"""密钥环与敏感值加密。

``data/secret.key`` **从第一天起就是一个密钥环文件**，而不是单把密钥：每行一把
Fernet key，**首行为当前加密用的 key**，其余仅用于解密历史数据。

为什么一开始就上密钥环：轮换密钥于是变成一次纯加法（在文件头部插一行新 key，
旧密文照常解得开），不需要停机、不需要一次性重加密全表。如果起初只存一把密钥，
将来轮换就得写迁移脚本，而那种脚本一旦中途失败就会留下一半解不开的数据。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from . import contract as C

log = logging.getLogger(__name__)


class KeyringMissing(RuntimeError):
    """密钥环文件不存在，而数据库里已经有密文。

    这是**拒绝启动**的情形，绝不静默重新生成——一次静默重生成会让 setting 表里的
    OpenRouter key 永久解不开，而且当时不会报任何错，等到下一次真正调用上游时才
    表现为一个莫名其妙的失败。这是一扇单向门。
    """


class KeyringInvalid(RuntimeError):
    """密钥环文件存在但内容不合法。"""


class Keyring:
    """一组 Fernet 密钥。首把用于加密，全部用于解密。"""

    __slots__ = ("_keys", "_mf", "path")

    def __init__(self, keys: list[bytes], path: Path | None = None) -> None:
        if not keys:
            raise KeyringInvalid("密钥环为空")
        try:
            fernets = [Fernet(k) for k in keys]
        except (ValueError, TypeError) as e:
            raise KeyringInvalid(
                f"密钥环里有不合法的 Fernet key：{e}。"
                "每行必须是 Fernet.generate_key() 的输出（44 字符 urlsafe-base64）。"
            ) from e
        self._keys = keys
        self._mf = MultiFernet(fernets)
        self.path = path

    # ------------------------------------------------------------- factories
    @classmethod
    def load_or_create(cls, path: Path, *, allow_create: bool = True) -> Keyring:
        """读取密钥环；不存在时按 ``allow_create`` 决定新建还是报错。

        调用方必须在 DB 里已有密文时传 ``allow_create=False``——见 :class:`KeyringMissing`。
        """
        if path.exists():
            return cls.load(path)
        if not allow_create:
            raise KeyringMissing(
                f"{path} 不存在，但数据库里已有加密数据。\n"
                "拒绝启动：重新生成密钥环会让已有密文永久无法解开。\n"
                "请从备份恢复该文件；若确认数据可弃，删除数据库后重新初始化。"
            )
        return cls.create(path)

    @classmethod
    def load(cls, path: Path) -> Keyring:
        raw = path.read_text(encoding="ascii")
        keys = [ln.strip().encode("ascii") for ln in raw.splitlines() if ln.strip()]
        if not keys:
            raise KeyringInvalid(f"{path} 是空的。若要重新初始化请先删除它。")
        return cls(keys, path)

    @classmethod
    def create(cls, path: Path) -> Keyring:
        """生成一把新密钥并落盘（0600）。

        用 ``Fernet.generate_key()`` 而不是 ``openssl rand -base64 32``——后者实测有
        很高概率产出含 ``+`` / ``/`` 的非规范 base64，在逐行解析的密钥环上是隐患。
        """
        key = Fernet.generate_key()
        path.parent.mkdir(parents=True, exist_ok=True)
        # 先以 0600 创建再写，避免存在一个短暂的宽权限窗口
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, C.FILE_MODE)
        with os.fdopen(fd, "wb") as f:
            f.write(key + b"\n")
        log.info("已生成新的密钥环 %s（权限 %o）", path, C.FILE_MODE)
        return cls([key], path)

    # ------------------------------------------------------------- rotation
    def rotate(self) -> bytes:
        """在头部插入一把新密钥，旧密钥保留用于解密。返回新密钥。

        纯加法：不触碰任何已有密文。旧密文在下次被写回时自然升级到新密钥。
        """
        if self.path is None:
            raise KeyringInvalid("内存密钥环不支持轮换")
        new = Fernet.generate_key()
        keys = [new, *self._keys]
        tmp = self.path.with_suffix(".key.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, C.FILE_MODE)
        with os.fdopen(fd, "wb") as f:
            f.write(b"\n".join(keys) + b"\n")
        os.replace(tmp, self.path)
        self.__init__(keys, self.path)  # type: ignore[misc]
        log.info("密钥环已轮换，现有 %d 把密钥", len(keys))
        return new

    # ------------------------------------------------------------- crypto
    def encrypt(self, plaintext: str) -> bytes:
        return self._mf.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, ciphertext: bytes) -> str:
        try:
            return self._mf.decrypt(ciphertext).decode("utf-8")
        except InvalidToken as e:
            raise KeyringInvalid(
                "无法解密：密钥环里没有能解开这份密文的密钥。"
                "通常意味着 secret.key 被替换过，或备份与数据库来自不同的部署。"
            ) from e

    def __len__(self) -> int:
        return len(self._keys)
