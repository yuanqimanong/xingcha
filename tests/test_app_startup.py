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

    def test_route_table_enumeration_actually_sees_everything(self, settings: Settings):
        """先证明枚举方式本身是对的。

        这条测试存在的理由：上一版的闭集断言写的是
        ``{r.path for r in app.routes if hasattr(r, "path")}``，而承载**全部**
        ``/v1`` 与 ``/admin`` 路由的两个 ``_IncludedRouter`` **没有 .path 属性**，
        被 ``hasattr`` 直接跳过——顶层只看得见 8 条，真实有 30+ 条。

        于是那条"闭集"断言对绝大多数路由完全不可见：往 /v1 下加一条免鉴权的
        泄漏端点，测试照样全绿。**枚举错了，后面所有断言都是装饰。**
        """
        routes = all_routes(create_app(settings))
        paths = {p for p, _ in routes}
        assert len(paths) > 20, f"只枚举到 {len(paths)} 条，递归没生效"
        # 三个层次各抽一条，证明确实穿透了 _IncludedRouter
        assert "/healthz" in paths
        assert "/v1/models" in paths
        assert "/admin/keys" in paths

    def test_unauthenticated_routes_are_a_closed_set(self, settings: Settings):
        """免鉴权路由是一个安全关键闭集。

        **按路径前缀判断是不够的**——那只是"看起来在 /admin 下面"，不是"真的要鉴权"。
        所以这里对**每一条**路由真发一次无凭据的请求，看谁放行。

        放行的定义：不是 401/403，也不是重定向到登录页。任何别的结果都算"这条路由
        在没有凭据时给出了内容"。
        """
        app = create_app(settings)
        opened: set[str] = set()
        with TestClient(app) as client:
            for path, methods in all_routes(app):
                if "{" in path:  # 带路径参数的先跳过，下一条测试专门覆盖
                    continue
                for method in methods:
                    if method in {"HEAD", "OPTIONS"}:
                        continue  # OPTIONS 免鉴权是契约要求的（CORS 预检）
                    r = client.request(method, path, follow_redirects=False)
                    if _is_closed(r):
                        continue
                    opened.add(f"{method} {path}")

        # 闭集。**改动它必须是一次显式决定。**
        allowed = {
            "GET /healthz",
            "GET /readyz",
            "GET /version",
            # 登录页本身不能要鉴权，否则没人进得去。
            #
            # ``POST /admin/login`` **不在这里**：空 body 会先撞 422，按 _is_closed
            # 的口径算"挡住了"。它的免鉴权性质由 TestLogin 那一组直接覆盖——
            # 这也说明 422 会掩盖一条路由是否开放，所以才需要
            # TestAdminMutationsEnforceAuth 那种填了合法 body 再打的测试。
            "GET /admin/login",
            # 静态资源：登录页自己要用 style.css 与 app.js，必须先于鉴权可取。
            # 里面只有手写的 CSS 与 JS，没有任何数据。
            "GET /admin/static",
            # FastAPI 的文档挪到了 /admin 下但**没有**鉴权。paths 为空（真实路由都在
            # 子路由器里），所以泄漏面可忽略；但它确实在闭集里，写出来才算数。
            "GET /admin/docs",
            "GET /admin/openapi.json",
        }
        assert opened == allowed, (
            f"免鉴权路由集合变了。\n  多出来：{sorted(opened - allowed)}"
            f"\n  少掉了：{sorted(allowed - opened)}"
        )

    def test_a_new_unauthenticated_route_turns_this_red(self, settings: Settings):
        """**反证：这条守卫真的守得住吗。**

        上一版的写法在这个实验下是绿的——因为它的枚举压根看不见 8 条以外的路由。
        所以把实验固化下来：顶层加一条免鉴权的泄漏端点，必须被发现。

        选顶层而不是 ``/v1`` 下：``/v1`` 的 catch-all 带着鉴权依赖且注册在最后，
        任何后加的 ``/v1/*`` 路由都会被它遮住并因此得到 401——那种"保护"是巧合
        而不是设计，但结果上确实挡住了。顶层没有这层兜底，正是真正的敞口所在。
        """
        from fastapi import APIRouter

        app = create_app(settings)
        leak = APIRouter()

        @leak.get("/debug-dump", include_in_schema=False)
        async def dump() -> dict[str, str]:
            return {"upstream_key": "sk-or-v1-LEAKED"}

        app.include_router(leak)

        opened = set()
        with TestClient(app) as client:
            for path, methods in all_routes(app):
                if "{" in path:
                    continue
                for method in methods:
                    if method in {"HEAD", "OPTIONS"}:
                        continue
                    if not _is_closed(client.request(method, path, follow_redirects=False)):
                        opened.add(f"{method} {path}")

        assert "GET /debug-dump" in opened, (
            "枚举没发现这条新加的免鉴权路由——说明上面那条闭集断言是装饰"
        )

    def test_every_v1_route_demands_credentials(self, settings: Settings):
        """``/v1`` 下**每一条**路由都要凭据（OPTIONS 除外，那是 CORS 预检）。

        单独一条是因为这是对外的付费面：/v1 上任何一处放行都等于把一把付费 key
        的调用权交给公网，而 catch-all 的兜底是"注册顺序造成的巧合"，不该被当成设计。
        """
        app = create_app(settings)
        checked = 0
        with TestClient(app) as client:
            for path, methods in all_routes(app):
                if not path.startswith("/v1") or "{" in path:
                    continue
                for method in methods:
                    if method in {"HEAD", "OPTIONS"}:
                        continue
                    r = client.request(method, path, follow_redirects=False)
                    checked += 1
                    assert r.status_code == 401, f"{method} {path} 无凭据时返回了 {r.status_code}"
                    assert r.json()["error"]["type"] == C.ErrorType.INVALID_API_KEY.value
        assert checked >= 2, f"只检查了 {checked} 条 /v1 路由，枚举可能没生效"


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


def all_routes(app: object) -> list[tuple[str, list[str]]]:
    """递归枚举 app 的**全部**路由，穿透 FastAPI 的 _IncludedRouter。

    ``app.routes`` 只给顶层：``include_router`` 进来的整批路由被折叠成一个
    ``_IncludedRouter`` 对象，而它**没有 .path 属性**。所以任何
    ``if hasattr(r, "path")`` 的写法都会静默漏掉绝大多数路由——上一版的闭集断言
    正是这么写的，于是它只看得见 8 条，而真实有 30+ 条。
    """

    def walk(routes: object, prefix: str = "") -> list[tuple[str, list[str]]]:
        out: list[tuple[str, list[str]]] = []
        for r in routes:  # type: ignore[union-attr]
            inner = getattr(r, "original_router", None)
            if inner is not None:
                ctx = getattr(r, "include_context", None)
                out += walk(inner.routes, prefix + (getattr(ctx, "prefix", "") or ""))
            elif hasattr(r, "path"):
                out.append((prefix + r.path, sorted(getattr(r, "methods", None) or ["GET"])))
        return out

    return walk(app.routes)  # type: ignore[attr-defined]


def _is_closed(response: object) -> bool:
    """这次无凭据的请求是否被挡住了。

    三种算挡住：
    - 401 / 403；
    - 重定向到登录页（后台的 HTML 页面走这条）；
    - **422**。这一条需要解释：FastAPI 在进入函数体之前先校验表单体，而后台的鉴权
      是在函数体里调 ``guard_mutation`` 的。所以一个空 body 的 POST 会先撞 422。
      它**没有执行任何动作**，只暴露了字段名，所以不算越权；但正因为鉴权在函数体
      里，"新加一个 admin POST 忘了调 guard_mutation" 不会被这条测试发现——
      那由 :meth:`TestAdminMutationsEnforceAuth` 专门覆盖。
    """
    code = response.status_code  # type: ignore[attr-defined]
    if code in {401, 403, 422}:
        return True
    if code in {302, 303, 307}:
        return "/admin/login" in (response.headers.get("location") or "")  # type: ignore[attr-defined]
    return False


class TestAdminMutationsEnforceAuth:
    """**每一条后台写操作，在给了格式合法的 body 时也必须无凭据被拒。**

    为什么单独一条：鉴权是在函数体里调 ``guard_mutation`` 的，不是路由依赖。
    这意味着新加一个 admin POST 忘了那一行，**没有任何东西会变红**——而闭集测试
    看到的是 422（body 校验先发生），会把它当成"已挡住"。

    这条测试自维护：先用空 body 探一次，从 422 的响应里读出缺哪些字段，填上假值
    再打一次。所以新增字段不用改测试，而漏掉鉴权一定会红。
    """

    def test_every_admin_post_rejects_without_a_session(self, settings: Settings):
        app = create_app(settings)
        checked, leaked = 0, []
        with TestClient(app) as client:
            for path, methods in all_routes(app):
                if "POST" not in methods or not path.startswith("/admin"):
                    continue
                if path == "/admin/login" or "{" in path:
                    continue  # 登录本身免鉴权；带路径参数的下一条覆盖

                probe = client.post(path, follow_redirects=False)
                form: dict[str, str] = {}
                if probe.status_code == 422:
                    for err in probe.json().get("detail", []):
                        loc = err.get("loc") or []
                        if len(loc) >= 2 and loc[0] == "body":
                            form[str(loc[1])] = "x"
                r = client.post(path, data=form or None, follow_redirects=False)
                checked += 1
                if not _is_closed(r):
                    leaked.append(f"POST {path} → {r.status_code}")

        assert checked >= 5, f"只检查了 {checked} 条后台写操作，枚举可能没生效"
        assert not leaked, f"这些后台写操作在无凭据时没有被拒：{leaked}"

    def test_parameterised_admin_mutations_too(self, settings: Settings):
        """带路径参数的那几条（回滚、导出）同样要挡。"""
        app = create_app(settings)
        with TestClient(app) as client:
            for method, path in [
                ("POST", "/admin/agents/whatever/rollback"),
                ("GET", "/admin/agents/whatever/export"),
                ("GET", "/admin/agents/whatever"),
            ]:
                r = client.request(method, path, follow_redirects=False)
                assert _is_closed(r), f"{method} {path} 无凭据时返回了 {r.status_code}"


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
