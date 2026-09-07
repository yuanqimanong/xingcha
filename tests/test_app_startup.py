"""启动序列与探针。

启动时的每一条断言都是**拒绝启动**而不是警告。这里逐条验证它们真的会拒绝——
一个"本该拒绝却放行了"的断言比没有断言更糟：它会让人以为有保护。
"""

from __future__ import annotations

import sqlite3
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from xingcha import contract as C
from xingcha.app import create_app
from xingcha.config import Settings
from xingcha.crypto import KeyringMissing
from xingcha.db import migrate
from xingcha.db.engine import StartupRefused, assert_single_worker


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data")


@pytest.fixture
def client(settings: Settings):
    with TestClient(create_app(settings)) as c:
        yield c


# =============================================================================
# 探针 —— 免鉴权闭集
# =============================================================================


class TestProbes:
    def test_healthz(self, client: TestClient):
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

    def test_readyz_reports_db_and_disk(self, client: TestClient):
        """磁盘要单独报：整个产品就是一个 SQLite 文件，磁盘一满就是写失败 +
        迁移失败 + 无法启动，而根因（通常是日志涨满）在别处完全看不见。"""
        r = client.get("/readyz")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["checks"]["db"] == "ok"
        assert "disk_free_pct" in body["checks"]

    def test_version_exposes_contract_and_features(self, client: TestClient):
        """契约协商入口。万一真要收紧某个行为，这是唯一的非硬切发布通道。"""
        body = client.get("/version").json()
        assert body["contract"] == C.CONTRACT_VERSION
        assert set(body["features"]) == set(C.FEATURES)

    def test_unauthenticated_routes_are_a_closed_set(self, settings: Settings):
        """免鉴权路由是一个安全关键闭集。

        新增一条免鉴权路由必须让这个测试变红——否则某天会有人"顺手"把一个
        管理端点放出去。
        """
        app = create_app(settings)
        paths = {r.path for r in app.routes if hasattr(r, "path")}  # type: ignore[attr-defined]
        public = {p for p in paths if not p.startswith(("/admin", "/api", "/v1"))}
        assert public == {"/healthz", "/readyz", "/version"}


# =============================================================================
# 启动断言
# =============================================================================


class TestStartupAssertions:
    def test_migration_runs_automatically(self, settings: Settings):
        """「重启即升级」的前提：serve 自己会把库升到最新。"""
        assert not settings.db_path.exists()
        with TestClient(create_app(settings)):
            pass
        assert settings.db_path.exists()
        from xingcha.db import migrate

        assert migrate.current_revision(settings.db_path) == migrate.head_revision()

    def test_data_dir_permissions(self, settings: Settings):
        """共享 VPS 上 0755 的数据目录 + 0644 的库文件，等于把 token hash 与
        Fernet 密文交给任意本地账号。"""
        with TestClient(create_app(settings)):
            pass
        assert stat.S_IMODE(settings.data_dir.stat().st_mode) == C.DIR_MODE
        assert stat.S_IMODE(settings.secret_path.stat().st_mode) == C.FILE_MODE

    def test_refuses_to_start_when_keyring_gone_but_ciphertext_exists(self, settings: Settings):
        """这是一扇单向门。

        静默重新生成密钥环会让 setting 表里的 OpenRouter key 永久解不开，而且当时
        不报任何错——等到下次真正调用上游时才表现为一个莫名其妙的失败。
        """
        # 先正常起一次，写入一条密文
        with TestClient(create_app(settings)):
            pass
        with sqlite3.connect(settings.db_path) as conn:
            conn.execute(
                "INSERT INTO setting (key, value_enc, is_secret, updated_at) "
                "VALUES ('openrouter.api_key', X'DEADBEEF', 1, '2026-01-01T00:00:00+00:00')"
            )

        # 密钥环丢失（误删、备份没带上、换机器忘了拷）
        settings.secret_path.unlink()

        with pytest.raises(KeyringMissing) as exc, TestClient(create_app(settings)):
            pass
        assert "拒绝启动" in str(exc.value)

    def test_creates_keyring_on_fresh_deploy(self, settings: Settings):
        """全新部署没有密文，正常创建，不该拦。"""
        with TestClient(create_app(settings)):
            pass
        assert settings.secret_path.exists()

    def test_single_worker_is_enforced(self):
        """并发上限、用量缓冲、SQLite 单写者都依赖单进程。

        改成 2 会让三者同时失效且症状互不相关——宁可起不来。
        """
        assert_single_worker(1)  # 不抛
        with pytest.raises(StartupRefused) as exc:
            assert_single_worker(2)
        assert "worker" in str(exc.value)


# =============================================================================
# 错误信封
# =============================================================================


class TestErrorEnvelope:
    def test_redact_keeps_the_key_class(self):
        """脱敏保留前缀：知道漏的是哪一类 key，才知道该吊销哪一把。

        脱敏的目的是别让密文进日志，不是让日志变得无法排查。
        """
        from xingcha.errors import redact

        leaky = "connect to https://openrouter.ai with key sk-or-v1-abcdefghijklmnop failed"
        cleaned = redact(leaky)
        assert "sk-or-v1-abcdefghijklmnop" not in cleaned
        assert "sk-or-v1-***" in cleaned

    def test_5xx_leaks_nothing_in_body_or_log(self, settings: Settings):
        """**真发一个会炸的请求**，同时断言响应体与日志都不含 key。

        这条测试原先只对纯函数 ``redact()`` 断言、从头到尾没发过请求——于是漏掉了
        真正的缺口：``unhandled_error_handler`` 里的 ``log.exception()`` 把整条
        traceback 原样写出去，而 ``redact()`` 只被用在 ``XingchaError.log_detail``
        上。实测结果是**响应体干净、日志里那把 key 逐字出现**。

        挡住了回显、没挡住日志——而日志会进 json-file、进 docker logs、进任何日志
        收集系统。所以断言必须同时覆盖两侧。
        """
        import io
        import logging

        from fastapi import APIRouter

        from xingcha.errors import RedactingFormatter

        KEY = "sk-or-v1-LEAKEDKEYabcdef123456"

        settings.ensure_data_dir()
        migrate.upgrade_to_head(settings.db_path, settings.backup_dir)
        app = create_app(settings)

        # 一条一定会炸的路由，异常文本里带着 key —— 这正是 httpx / openai 抛出来的形状
        router = APIRouter()

        @router.get("/_boom", include_in_schema=False)
        async def boom() -> None:
            raise RuntimeError(f"connect https://openrouter.ai key {KEY} failed")

        app.include_router(router)

        # 把根 logger 的输出接到内存里。formatter 用生产那一个，不是测试专用的。
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(RedactingFormatter("%(levelname)s %(name)s | %(message)s"))
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                r = client.get("/_boom")
        finally:
            root.removeHandler(handler)

        assert r.status_code == 500
        body = r.text
        assert KEY not in body, "响应体泄漏了上游 key"
        assert r.json()["error"]["type"] == C.ErrorType.INTERNAL_ERROR.value

        logged = stream.getvalue()
        assert "Traceback" in logged, "异常没有被记进日志？那排查就没抓手了"
        assert KEY not in logged, f"**日志里泄漏了上游 key**：{logged[:400]}"
        assert "sk-or-v1-***" in logged, "脱敏后应当留下前缀，指明漏的是哪类 key"

    def test_redacts_own_tokens_too(self):
        """自家的 sk-xc- 同样要脱敏 —— 日志泄漏一样能被用来调用。"""
        from xingcha.errors import redact

        out = redact("bad token sk-xc-1-a1b2c3d4e5f60718-" + "x" * 43)
        assert "sk-xc-***" in out
        assert "a1b2c3d4e5f60718" not in out

    def test_error_bodies_follow_openai_shape(self):
        """调用方用的是 OpenAI SDK，它按 error.type 分支。"""
        from xingcha.errors import ModelNotFound

        body = ModelNotFound("nope").to_body()
        assert set(body["error"]) >= {"message", "type", "code", "param"}
        assert body["error"]["type"] == C.ErrorType.MODEL_NOT_FOUND.value

    def test_auth_errors_are_indistinguishable(self):
        """区分 token 无效/禁用/过期 = 给公网一个 token 有效性 oracle。"""
        from xingcha.errors import InvalidApiKey

        expired = InvalidApiKey(log_detail="token 已过期").to_body()
        disabled = InvalidApiKey(log_detail="token 已禁用").to_body()
        assert expired == disabled  # 对外完全一致


class TestKeyringGuardIsShared:
    """密钥环守卫必须覆盖**所有**入口，不能只在 serve 里有。

    这是一次真实的疏漏：CLI 的 _bootstrap 曾经用默认的 allow_create=True，于是密钥环
    丢失后跑一次 `xingcha config get` 就会静默重新生成——不但绕过单向门，还让服务
    重新「能启动」，而库里的密文已永久解不开。

    根因是同一条规则写在了两处。现在两个入口都走 bootstrap.prepare。
    """

    def _seed_ciphertext(self, settings: Settings) -> None:
        with TestClient(create_app(settings)):
            pass
        with sqlite3.connect(settings.db_path) as conn:
            conn.execute(
                "INSERT INTO setting (key, value_enc, is_secret, updated_at) "
                "VALUES ('openrouter.api_key', X'DEADBEEF', 1, '2026-01-01T00:00:00+00:00')"
            )
        settings.secret_path.unlink()

    def test_cli_bootstrap_also_refuses(self, settings: Settings, monkeypatch):
        from xingcha import bootstrap, config

        self._seed_ciphertext(settings)
        monkeypatch.setattr(config, "_settings", settings)
        with pytest.raises(KeyringMissing):
            bootstrap.prepare(settings)

    def test_detects_ciphertext_without_async_engine(self, settings: Settings):
        """判断必须在打开密钥环之前完成，那时异步引擎还没建起来。"""
        from xingcha.bootstrap import db_has_ciphertext

        assert db_has_ciphertext(settings.db_path) is False  # 库都还不存在
        self._seed_ciphertext(settings)
        assert db_has_ciphertext(settings.db_path) is True

    def test_only_one_place_decides(self):
        """守卫只能有一处实现。

        任何直接调用 Keyring.load_or_create 的地方都可能漏掉 allow_create——
        所以除了 bootstrap.py 与 crypto.py 自身，其它模块不许出现这个调用。
        """
        import pathlib

        import xingcha

        root = pathlib.Path(xingcha.__file__).parent
        offenders = [
            f.relative_to(root)
            for f in root.rglob("*.py")
            if f.name not in {"bootstrap.py", "crypto.py"}
            and "load_or_create" in f.read_text(encoding="utf-8")
        ]
        assert not offenders, f"这些模块绕过了共享守卫：{offenders}"
