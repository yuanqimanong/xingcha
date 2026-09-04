"""密钥环测试。

最重要的一条是 :meth:`TestRefuseToRegenerate` —— 那扇单向门：一次静默重生成会让
setting 表里的 OpenRouter key 永久解不开，而且当时不报任何错，等到下次真正调用上游
时才表现为一个莫名其妙的失败。
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from xingcha import contract as C
from xingcha.crypto import Keyring, KeyringInvalid, KeyringMissing


class TestCreateAndLoad:
    def test_creates_with_0600(self, tmp_path: Path):
        """共享 VPS 上一把 0644 的密钥文件等于没加密。"""
        kr = Keyring.create(tmp_path / "secret.key")
        mode = stat.S_IMODE((tmp_path / "secret.key").stat().st_mode)
        assert mode == C.FILE_MODE
        assert len(kr) == 1

    def test_roundtrip(self, tmp_path: Path):
        kr = Keyring.create(tmp_path / "k.key")
        assert kr.decrypt(kr.encrypt("sk-or-v1-secret")) == "sk-or-v1-secret"

    def test_load_existing(self, tmp_path: Path):
        p = tmp_path / "k.key"
        original = Keyring.create(p)
        blob = original.encrypt("hello")
        assert Keyring.load(p).decrypt(blob) == "hello"

    def test_load_or_create_is_stable(self, tmp_path: Path):
        p = tmp_path / "k.key"
        blob = Keyring.load_or_create(p).encrypt("x")
        # 第二次必须读回同一把，而不是生成新的
        assert Keyring.load_or_create(p).decrypt(blob) == "x"

    def test_rejects_empty_file(self, tmp_path: Path):
        p = tmp_path / "k.key"
        p.write_text("", encoding="ascii")
        with pytest.raises(KeyringInvalid):
            Keyring.load(p)

    def test_rejects_non_canonical_base64(self, tmp_path: Path):
        """不要用 openssl rand -base64 32 生成密钥行。

        实测它有很高概率产出含 + / 的非规范 base64，在逐行解析的密钥环上是隐患。
        Fernet.generate_key() 产出的是 urlsafe 变体。
        """
        p = tmp_path / "k.key"
        p.write_text("not-a-valid-fernet-key\n", encoding="ascii")
        with pytest.raises(KeyringInvalid):
            Keyring.load(p)


class TestRefuseToRegenerate:
    def test_refuses_when_ciphertext_exists(self, tmp_path: Path):
        """这是一扇单向门：静默重生成 = OpenRouter key 永久解不开，且当时不报错。"""
        with pytest.raises(KeyringMissing) as exc:
            Keyring.load_or_create(tmp_path / "gone.key", allow_create=False)
        # 报错必须告诉运维该怎么办，而不只是说"失败了"
        msg = str(exc.value)
        assert "备份" in msg
        assert "拒绝启动" in msg

    def test_creates_when_no_ciphertext(self, tmp_path: Path):
        """全新部署：没有密文，正常创建。"""
        kr = Keyring.load_or_create(tmp_path / "new.key", allow_create=True)
        assert len(kr) == 1


class TestRotation:
    def test_rotation_is_purely_additive(self, tmp_path: Path):
        """轮换不触碰任何已有密文——这正是一开始就上密钥环的理由。

        如果起初只存一把密钥，轮换就得写一个"重加密全表"的迁移脚本，
        而那种脚本一旦中途失败就会留下一半解不开的数据。
        """
        p = tmp_path / "k.key"
        kr = Keyring.create(p)
        old_blob = kr.encrypt("配置里的旧值")

        kr.rotate()
        assert len(kr) == 2
        # 旧密文照常解得开
        assert kr.decrypt(old_blob) == "配置里的旧值"
        # 新写入用的是新密钥
        new_blob = kr.encrypt("新值")
        assert kr.decrypt(new_blob) == "新值"

    def test_rotation_survives_reload(self, tmp_path: Path):
        p = tmp_path / "k.key"
        kr = Keyring.create(p)
        old_blob = kr.encrypt("v1")
        kr.rotate()
        reloaded = Keyring.load(p)
        assert len(reloaded) == 2
        assert reloaded.decrypt(old_blob) == "v1"

    def test_rotated_file_keeps_0600(self, tmp_path: Path):
        p = tmp_path / "k.key"
        Keyring.create(p).rotate()
        assert stat.S_IMODE(p.stat().st_mode) == C.FILE_MODE


class TestWrongKeyring:
    def test_foreign_ciphertext_fails_loudly(self, tmp_path: Path):
        """备份与数据库来自不同部署时，必须明确报错而不是返回垃圾。"""
        a = Keyring([Fernet.generate_key()])
        b = Keyring([Fernet.generate_key()])
        blob = a.encrypt("secret")
        with pytest.raises(KeyringInvalid) as exc:
            b.decrypt(blob)
        assert "secret.key" in str(exc.value)
