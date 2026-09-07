"""浏览器端到端的夹具。

------------------------------------------------------------------------------
为什么要这一层
------------------------------------------------------------------------------

前面所有测试打的都是 ASGI 应用（TestClient）——验证的是"路由与模板对"。它们证明不了
**页面在真浏览器里能不能用**，而这两件事已经分叉过一次：CSP 是 ``script-src 'self'``
无 nonce，而模板里放着内联 ``<script>`` 与 ``onsubmit=``。TestClient 完全不在意，
浏览器把它们全部拒绝执行，于是"复制密钥"按钮无反应、危险操作的二次确认根本不弹。

测试守住了 CSP 头，也守住了模板里没有内联脚本——但**没有任何东西证明按钮真的会动**。
这一层就是补这个：起真服务、开真浏览器、点真按钮、读真控制台。

用系统的 /usr/bin/chromium，不下载 Playwright 自带的浏览器（离线可跑）。
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import uvicorn

from xingcha import contract as C
from xingcha.app import create_app
from xingcha.config import Settings
from xingcha.crypto import Keyring
from xingcha.db import migrate
from xingcha.db.engine import make_engine, make_sessionmaker
from xingcha.services import auth as auth_svc
from xingcha.services import setting as setting_svc
from xingcha.services import websession as ws

#: 后台密码。至少 12 位（登录页自己会拦短的）。
ADMIN_PASSWORD = "e2e-password-2026"

CHROMIUM = "/usr/bin/chromium"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass
class LiveSite:
    """一个跑着的星槎，外加它的地址与已备好的凭据。"""

    base_url: str
    settings: Settings
    upstream_base: str
    #: 已设好密码，可直接登录。
    password: str = ADMIN_PASSWORD

    def wait_for_usage_flush(self, *, timeout: float = 15.0) -> None:
        """等用量缓冲落盘。

        用量不是同步写库的（A9：批量 flush，另有 5 秒周期 flush）。轮询库而不是
        睡一个固定时长：睡短了偶发失败，睡长了每条测试都在白等。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.db_rows("SELECT 1 FROM run LIMIT 1"):
                return
            time.sleep(0.3)
        raise AssertionError(f"等了 {timeout}s，run 表里还是空的——用量没有落盘")

    def db_rows(self, sql: str) -> list[dict]:
        import sqlite3

        with sqlite3.connect(self.settings.db_path) as c:
            c.row_factory = sqlite3.Row
            return [dict(r) for r in c.execute(sql).fetchall()]


def run_async(make_coro) -> None:
    """在**独立线程的独立 loop** 里跑一段异步初始化。

    不能用 ``asyncio.run``：pytest-asyncio 的 auto 模式在夹具阶段已经有一个运行中的
    loop，``asyncio.run`` 会直接抛 "cannot be called from a running event loop"。
    也不能用 ``new_event_loop().run_until_complete``——同一线程里已有 loop 在跑时
    它同样不行。

    收 callable 而不是 coroutine：数据库引擎必须在**将要跑它的那个 loop 里**创建，
    在主线程建好再丢过去会绑到错误的 loop 上。
    """
    box: list[BaseException] = []

    def worker() -> None:
        try:
            asyncio.run(make_coro())
        except BaseException as e:
            box.append(e)

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    if box:
        raise box[0]


def _serve(app, port: int) -> uvicorn.Server:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        if server.started:
            return server
        time.sleep(0.05)
    raise RuntimeError("服务没能启动")


@pytest.fixture(scope="session")
def browser():
    """一个真浏览器。整个会话共用，每个用例开新 context 保证 cookie 隔离。"""
    from playwright.sync_api import sync_playwright

    if not Path(CHROMIUM).exists():  # pragma: no cover
        pytest.skip(f"没有 {CHROMIUM}")
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
        yield b
        b.close()


@pytest.fixture
def site(tmp_path: Path, upstream) -> Iterator[LiveSite]:
    """起一个真的星槎 HTTP 服务，上游指向假上游，管理员密码已设好。

    密码在这里就设好（而不是每个用例自己走一遍首次设置流程）：那条流程有专门的
    用例覆盖，其余用例不该为它付时间。
    """
    settings = Settings(
        data_dir=tmp_path / "data",
        request_timeout=10.0,
        catalog_ttl_seconds=60,
        # 关掉 debounce：断言"上游吐几片就收几帧"时不受合帧影响
        stream_debounce_seconds=None,
    )
    settings.ensure_data_dir()
    migrate.upgrade_to_head(settings.db_path, settings.backup_dir)
    keyring = Keyring.load_or_create(settings.secret_path)

    async def seed() -> None:
        engine = make_engine(settings.db_path)
        maker = make_sessionmaker(engine)
        async with maker() as s:
            await setting_svc.set_(s, keyring, C.SETTING_KEY_OPENROUTER_API_KEY, "sk-or-v1-e2e")
            await setting_svc.set_(s, keyring, C.SETTING_KEY_OPENROUTER_BASE_URL, upstream.base_url)
            admin = await ws.get_admin(s)
            assert admin is not None
            admin.password_hash = ws.hash_password(ADMIN_PASSWORD)
            await auth_svc.issue(s, name="已有的一把")
            await s.commit()
        await engine.dispose()

    run_async(seed)
    upstream.reset()

    port = free_port()
    server = _serve(create_app(settings), port)
    try:
        yield LiveSite(f"http://127.0.0.1:{port}", settings, upstream.base_url)
    finally:
        server.should_exit = True
        time.sleep(0.2)


@pytest.fixture
def fresh_site(tmp_path: Path, upstream) -> Iterator[LiveSite]:
    """一个**没有设过密码**的星槎，用来测首次初始化流程。"""
    settings = Settings(data_dir=tmp_path / "fresh", request_timeout=10.0, catalog_ttl_seconds=60)
    settings.ensure_data_dir()
    migrate.upgrade_to_head(settings.db_path, settings.backup_dir)
    keyring = Keyring.load_or_create(settings.secret_path)

    async def seed() -> None:
        engine = make_engine(settings.db_path)
        maker = make_sessionmaker(engine)
        async with maker() as s:
            await setting_svc.set_(s, keyring, C.SETTING_KEY_OPENROUTER_BASE_URL, upstream.base_url)
            await s.commit()
        await engine.dispose()

    run_async(seed)
    port = free_port()
    server = _serve(create_app(settings), port)
    try:
        yield LiveSite(f"http://127.0.0.1:{port}", settings, upstream.base_url)
    finally:
        server.should_exit = True
        time.sleep(0.2)


class Console:
    """收集页面控制台里的错误。

    **每个用例都要断言它是空的。** CSP 拦截、JS 异常、取不到的静态资源都只在这里
    露头——TestClient 那一层永远看不见，而它们恰恰是"页面看着对、按钮不работа"
    这类问题的唯一信号。
    """

    def __init__(self) -> None:
        self.errors: list[str] = []

    def attach(self, page) -> None:
        page.on(
            "console",
            lambda m: self.errors.append(f"[{m.type}] {m.text}") if m.type == "error" else None,
        )
        page.on("pageerror", lambda e: self.errors.append(f"[pageerror] {e}"))

    def assert_clean(self, allowed: list[str] | None = None) -> None:
        unexpected = [e for e in self.errors if not any(pat in e for pat in (allowed or []))]
        assert not unexpected, "浏览器控制台有错误：\n  " + "\n  ".join(unexpected)


@pytest.fixture
def page(browser, request):
    """一个新页面 + 挂好的控制台收集器。

    退出时自动断言控制台干净——**不用每个用例自己记得调**，忘了调的用例等于没测。
    """
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    pg = ctx.new_page()
    console = Console()
    console.attach(pg)
    pg.console_errors = console  # type: ignore[attr-defined]
    yield pg
    failed = getattr(request.node, "_e2e_failed", False)
    # 有些用例**故意**触发失败请求（比如断言吊销后的 key 真的 401），浏览器会把它
    # 记成 console error。用标记显式豁免，而不是把整条控制台断言放宽——
    # 放宽的话所有用例都会失去这层保护。
    allowed = [
        pat for mark in request.node.iter_markers("expect_console_errors") for pat in mark.args
    ]
    try:
        if not failed:
            console.assert_clean(allowed)
    finally:
        ctx.close()


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """用例本身失败时不要再叠加一条控制台断言失败——那会盖住真正的原因。"""
    outcome = yield
    report = outcome.get_result()
    if report.when == "call" and report.failed:
        item._e2e_failed = True


def login(page, site: LiveSite) -> None:
    """走一遍真实的登录（填表 + 点按钮），不走注入 cookie 的捷径。

    注入 cookie 会跳过 CSRF、会话签发与重定向——而那几步恰恰是最容易坏的。
    """
    page.goto(f"{site.base_url}/admin/login", wait_until="networkidle")
    page.fill("#password", site.password)
    page.click("button[type=submit]")
    page.wait_for_url(f"{site.base_url}/admin", wait_until="networkidle")


def json_of(page, url: str) -> dict:
    """在页面上下文里取一个 JSON 接口（带上会话 cookie）。"""
    return json.loads(
        page.evaluate("u => fetch(u, {credentials:'same-origin'}).then(r => r.text())", url)
    )
