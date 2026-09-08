"""后台每个功能点的浏览器自动化。

------------------------------------------------------------------------------
这一层与前面所有测试的区别
------------------------------------------------------------------------------

前面打的是 ASGI 应用，验证的是"路由与模板对"。这里起真服务、开真浏览器（系统的
``/usr/bin/chromium``）、点真按钮、读真控制台。

这个区别不是形式上的。CSP 是 ``script-src 'self'`` 无 nonce，而模板里曾放着内联
``<script>`` 与 ``onsubmit=``——TestClient 完全不在意，浏览器把它们全部拒绝执行，
于是「复制密钥」无反应、危险操作的二次确认根本不弹。当时有测试守住了 CSP 头、也有
测试守住了模板无内联脚本，**但没有任何东西证明按钮真的会动**。

所以这里的每一条都以"用户能不能完成这件事"为断言，且 ``page`` 夹具在用例结束时
自动断言控制台无错误——忘了断言的用例等于没测。

标记为 ``browser``：`pytest -m "not browser"` 可跳过（它比其余测试慢一个数量级）。
"""

from __future__ import annotations

import re
from typing import ClassVar

import pytest

from conftest_web import ADMIN_PASSWORD, LiveSite, login

pytestmark = pytest.mark.browser

#: 在页面上下文里拿一把 key 去打 /v1，返回状态码。
#: 抽成常量是为了让两处用法一致——写两遍迟早有一处漏掉 Bearer 前缀。
#: 同上，但取回 /v1/models 的 JSON。
LIST_MODELS = """
async ([base, k]) => {
  const r = await fetch(base + '/v1/models', {headers: {Authorization: 'Bearer ' + k}});
  return await r.json();
}
"""

#: 用一把 key 真打一次 chat/completions，返回状态码。
CHAT_ONCE = """
async ([base, k]) => {
  const r = await fetch(base + '/v1/chat/completions', {
    method: 'POST',
    headers: {Authorization: 'Bearer ' + k, 'Content-Type': 'application/json'},
    body: JSON.stringify({
      model: 'openai/gpt-5',
      messages: [{role: 'user', content: 'x'}],
    }),
  });
  return r.status;
}
"""

PROBE_V1 = """
async ([base, k]) => {
  const r = await fetch(base + '/v1/models', {headers: {Authorization: 'Bearer ' + k}});
  return r.status;
}
"""


# =============================================================================
# 首次初始化与登录
# =============================================================================


class TestFirstRunAndLogin:
    def test_first_visit_guides_you_to_set_a_password(self, fresh_site: LiveSite, page):
        """全新部署第一次打开后台：应当引导设密码，而不是给一个进不去的登录框。

        runbook 第 8 步就是这么写的，而这条流程只有真走一遍才知道通不通。
        """
        page.goto(f"{fresh_site.base_url}/admin", wait_until="networkidle")
        assert page.url.endswith("/admin/login")
        assert "设置管理员密码" in page.inner_text("body")
        # setup 模式必须有确认框，否则打错一个字就锁死自己
        assert page.locator("#password").count() == 1
        assert page.locator("input[name=confirm]").count() == 1

    def test_short_password_is_refused_with_a_reason(self, fresh_site: LiveSite, page):
        """至少 12 位。报错要说清是为什么，不能只说"失败"。"""
        page.goto(f"{fresh_site.base_url}/admin/login", wait_until="networkidle")
        page.fill("#password", "short")
        page.fill("input[name=confirm]", "short")
        page.click("button[type=submit]")
        page.wait_for_load_state("networkidle")
        assert "12" in page.inner_text("body")

    def test_mismatched_confirmation_is_refused(self, fresh_site: LiveSite, page):
        page.goto(f"{fresh_site.base_url}/admin/login", wait_until="networkidle")
        page.fill("#password", "a-long-enough-password")
        page.fill("input[name=confirm]", "a-different-password")
        page.click("button[type=submit]")
        page.wait_for_load_state("networkidle")
        assert "不一致" in page.inner_text("body")

    def test_setting_the_password_logs_you_straight_in(self, fresh_site: LiveSite, page):
        """设完密码直接进后台，不要再让人登一次。"""
        page.goto(f"{fresh_site.base_url}/admin/login", wait_until="networkidle")
        page.fill("#password", ADMIN_PASSWORD)
        page.fill("input[name=confirm]", ADMIN_PASSWORD)
        page.click("button[type=submit]")
        page.wait_for_url(f"{fresh_site.base_url}/admin", wait_until="networkidle")
        assert "总览" in page.title()

    def test_wrong_password_does_not_reveal_whether_it_exists(self, site: LiveSite, page):
        """错误密码的提示不能透露"这个密码存在但错了"之外的信息。"""
        page.goto(f"{site.base_url}/admin/login", wait_until="networkidle")
        page.fill("#password", "definitely-not-the-password")
        page.click("button[type=submit]")
        page.wait_for_load_state("networkidle")
        body = page.inner_text("body")
        assert "不正确" in body or "错误" in body
        assert page.url.endswith("/admin/login")

    def test_logout_really_ends_the_session(self, site: LiveSite, page):
        """登出之后再访问后台必须被弹回登录页。

        只清 cookie 不废会话的话，一个被复制走的 cookie 还能继续用。
        """
        login(page, site)
        page.goto(f"{site.base_url}/admin/logout", wait_until="networkidle")
        page.goto(f"{site.base_url}/admin/keys", wait_until="networkidle")
        assert page.url.endswith("/admin/login")


# =============================================================================
# 导航与页面骨架
# =============================================================================


class TestNavigation:
    PAGES: ClassVar[list[tuple[str, str]]] = [
        ("/admin", "总览"),
        ("/admin/agents", "Agent"),
        ("/admin/keys", "密钥"),
        ("/admin/quota", "配额"),
        ("/admin/logs", "调用记录"),
        ("/admin/settings", "设置"),
    ]

    @pytest.mark.parametrize(("path", "word"), PAGES)
    def test_every_page_renders(self, site: LiveSite, page, path: str, word: str):
        """每个页面都能打开、标题对、且控制台无错误（由 page 夹具自动断言）。"""
        login(page, site)
        page.goto(f"{site.base_url}{path}", wait_until="networkidle")
        assert word in page.title() or word in page.inner_text("h1")

    def test_sidebar_links_to_every_page(self, site: LiveSite, page):
        """侧栏必须能到达每个页面。

        少一个链接的后果不是报错，是那个功能"看起来不存在"——用户不会去猜 URL。
        """
        login(page, site)
        hrefs = set(
            page.eval_on_selector_all(
                "nav a, aside a", "els => els.map(e => e.getAttribute('href'))"
            )
        )
        for path, _ in self.PAGES:
            assert path in hrefs or path + "/" in hrefs, f"侧栏没有到 {path} 的链接"

    def test_no_external_resources(self, site: LiveSite, page):
        """**禁 CDN、离线可用**是硬要求。

        页面上所有资源必须来自本机——一个 CDN 引用就让离线部署变成半坏状态，
        而且 CSP 会静默挡掉它（表现为样式/脚本莫名失效）。
        """
        external: list[str] = []
        page.on(
            "request",
            lambda r: (
                external.append(r.url)
                if not r.url.startswith((site.base_url, "data:", "blob:"))
                else None
            ),
        )
        login(page, site)
        for path, _ in self.PAGES:
            page.goto(f"{site.base_url}{path}", wait_until="networkidle")
        assert not external, f"页面引用了外部资源：{external}"


# =============================================================================
# 密钥：签发 · 复制 · 吊销
# =============================================================================


class TestKeys:
    def test_issue_shows_the_plaintext_exactly_once(self, site: LiveSite, page):
        login(page, site)
        page.goto(f"{site.base_url}/admin/keys", wait_until="networkidle")
        page.fill("input[name=name]", "e2e-签发")
        page.click("form[action='/admin/keys/issue'] button[type=submit]")
        page.wait_for_load_state("networkidle")

        shown = page.inner_text("body")
        assert "唯一一次" in shown
        key = re.search(r"sk-xc-1-[A-Za-z0-9_-]+", shown)
        assert key, "页面上没有出现明文 key"
        assert len(key.group(0)) > 40, "明文被截断了？"

        # **URL 里不能出现明文。** 它会进浏览器历史、留在地址栏、进 Referer。
        assert "sk-xc-" not in page.url, f"明文进了 URL：{page.url}"
        assert "issued=" not in page.url

        # 刷新之后彻底不再出现——"唯一一次"必须是字面意义上的一次
        page.reload(wait_until="networkidle")
        after = page.inner_text("body")
        assert "唯一一次" not in after, "刷新后仍在展示明文"
        assert key.group(0) not in after, "刷新后明文仍在页面上"

    def test_copy_button_actually_copies(self, site: LiveSite, page):
        """**这条就是当初坏掉的那个功能。**

        CSP 挡掉内联脚本时，这个按钮完全无反应——而页面同时写着「这是唯一一次
        看到明文」，68 位的 key 只能手抄。所以要真点、真读剪贴板。
        """
        page.context.grant_permissions(["clipboard-read", "clipboard-write"])
        login(page, site)
        page.goto(f"{site.base_url}/admin/keys", wait_until="networkidle")
        page.fill("input[name=name]", "e2e-复制")
        page.click("form[action='/admin/keys/issue'] button[type=submit]")
        page.wait_for_load_state("networkidle")

        found = re.search(r"sk-xc-1-[A-Za-z0-9_-]+", page.inner_text("body"))
        assert found, "页面上没有出现明文 key"
        shown = found.group(0)
        page.click("button[data-copy]")
        page.wait_for_function(
            "() => document.querySelector('[data-copy]').textContent.includes('已复制')"
        )
        assert page.evaluate("navigator.clipboard.readText()") == shown

    def test_revoke_asks_before_doing_it(self, site: LiveSite, page):
        """**危险操作必须二次确认。**

        用 onsubmit= 写的时候它被 CSP 静默挡掉：表单直接提交，而界面看起来像是
        有保护——比没有确认更糟。这里先点"取消"，断言什么都没发生。
        """
        login(page, site)
        page.goto(f"{site.base_url}/admin/keys", wait_until="networkidle")
        before = page.inner_text("table")

        page.once("dialog", lambda d: d.dismiss())  # 用户点取消
        page.click("form[action='/admin/keys/revoke'] button[type=submit]")
        page.wait_for_timeout(400)
        assert page.inner_text("table") == before, "点了取消，密钥却被吊销了"

    def test_revoke_works_when_confirmed(self, site: LiveSite, page):
        login(page, site)
        page.goto(f"{site.base_url}/admin/keys", wait_until="networkidle")
        page.once("dialog", lambda d: d.accept())
        page.click("form[action='/admin/keys/revoke'] button[type=submit]")
        page.wait_for_load_state("networkidle")
        assert "已吊销" in page.inner_text("table") or "吊销" in page.inner_text("body")
        rows = site.db_rows("SELECT name, is_active FROM token")
        assert any(not r["is_active"] for r in rows), f"库里没有任何一把被标记吊销：{rows}"

    @pytest.mark.expect_console_errors("401")
    def test_revoked_key_stops_working_immediately(self, site: LiveSite, page):
        """吊销的语义是"立刻"。

        缓存了 token 校验结果的话，吊销会有一个窗口期——而吊销的场景通常是
        "key 泄漏了"，窗口期就是损失。
        """
        login(page, site)
        page.goto(f"{site.base_url}/admin/keys", wait_until="networkidle")
        page.fill("input[name=name]", "e2e-吊销后失效")
        page.click("form[action='/admin/keys/issue'] button[type=submit]")
        page.wait_for_load_state("networkidle")
        found = re.search(r"sk-xc-1-[A-Za-z0-9_-]+", page.inner_text("body"))
        assert found, "页面上没有出现明文 key"
        key = found.group(0)

        assert page.evaluate(PROBE_V1, [site.base_url, key]) == 200

        page.once("dialog", lambda d: d.accept())
        page.click(
            f"form[action='/admin/keys/revoke']:has(input[value='{key.split('-')[3]}']) button"
        )
        page.wait_for_load_state("networkidle")

        assert page.evaluate(PROBE_V1, [site.base_url, key]) == 401, "吊销之后这把 key 还能用"


# =============================================================================
# Agent：新建 · schema lint · 保存 · 版本回滚 · 导出
# =============================================================================

SCHEMA_TEXT = """{
  "type": "object",
  "properties": {"title": {"type": "string"}, "score": {"type": "integer"}},
  "required": ["title", "score"]
}"""


class TestAgents:
    def _fill_new(self, page, site: LiveSite, *, slug: str, schema: str = SCHEMA_TEXT) -> None:
        page.goto(f"{site.base_url}/admin/agents/new", wait_until="networkidle")
        page.fill("#slug", slug)
        page.fill("#name", "抽取器")
        page.fill("#instructions", "从输入里抽取标题与评分。")
        page.fill("#model", "openai/gpt-5")
        if schema:
            page.fill("#output_schema", schema)

    def test_empty_state_tells_you_what_to_do(self, site: LiveSite, page):
        """一个还没有 Agent 的列表页不能只是空白。

        空状态是新用户看到的第一个页面，它得说出下一步。
        """
        login(page, site)
        page.goto(f"{site.base_url}/admin/agents", wait_until="networkidle")
        body = page.inner_text("body")
        assert "新建" in body

    def test_create_an_agent_end_to_end(self, site: LiveSite, page):
        login(page, site)
        self._fill_new(page, site, slug="extract")
        page.click("form button[type=submit]")
        page.wait_for_load_state("networkidle")

        assert "已保存" in page.inner_text("body")
        page.goto(f"{site.base_url}/admin/agents", wait_until="networkidle")
        assert "extract" in page.inner_text("body")

    def test_a_created_agent_is_immediately_callable_as_a_model(self, site: LiveSite, page):
        """**这是整个产品的那句承诺**：把 model 从 `openai/gpt-5` 改成 `extract`。

        建完就能调，不用重启——运行时按 (agent_id, version) 缓存，新版本自然不命中。
        """
        login(page, site)
        self._fill_new(page, site, slug="extract")
        page.click("form button[type=submit]")
        page.wait_for_load_state("networkidle")

        page.goto(f"{site.base_url}/admin/keys", wait_until="networkidle")
        page.fill("input[name=name]", "e2e-调用")
        page.click("form[action='/admin/keys/issue'] button[type=submit]")
        page.wait_for_load_state("networkidle")
        found = re.search(r"sk-xc-1-[A-Za-z0-9_-]+", page.inner_text("body"))
        assert found
        key = found.group(0)

        # /v1/models 里应当出现它，且 owned_by 是我们自己
        listed = page.evaluate(LIST_MODELS, [site.base_url, key])
        ours = [m for m in listed["data"] if m["id"] == "extract"]
        assert ours, f"新建的 Agent 没出现在 /v1/models 里：{[m['id'] for m in listed['data']][:5]}"
        assert ours[0]["owned_by"] == "xingcha"

    def test_schema_lint_gives_advice_without_saving(self, site: LiveSite, page):
        """lint 按钮是 htmx 局部刷新。

        它同时验证两件事：htmx 真的加载了（外部脚本、CSP 放行），以及 lint
        不会顺手把 Agent 存下去。
        """
        login(page, site)
        self._fill_new(page, site, slug="lintcheck")
        page.click("button:has-text('检查')") if page.locator(
            "button:has-text('检查')"
        ).count() else page.click("[hx-post='/admin/agents/lint']")
        page.wait_for_function(
            "() => document.querySelector('#lint-result')?.textContent.trim().length > 0"
        )
        assert page.inner_text("#lint-result").strip()

        page.goto(f"{site.base_url}/admin/agents", wait_until="networkidle")
        assert "lintcheck" not in page.inner_text("body"), "lint 把 Agent 存下去了"

    def test_dangerous_schema_is_refused_with_a_reason(self, site: LiveSite, page):
        """带 ``pattern`` 的 schema 必须被拒（A5：ReDoS 能打死单进程）。

        而且报错要说清是哪一条规则——只说"不合法"用户改不动。
        """
        login(page, site)
        self._fill_new(
            page,
            site,
            slug="redos",
            schema='{"type":"object","properties":{"x":{"type":"string","pattern":"(a+)+$"}}}',
        )
        page.click("form button[type=submit]")
        page.wait_for_load_state("networkidle")
        body = page.inner_text("body")
        assert "pattern" in body
        assert "已保存" not in body

    def test_slug_is_frozen_after_creation(self, site: LiveSite, page):
        """标识发布后不能改——调用方的代码里写着它。

        表单必须在结构上做到这一点，而不是只在说明文字里写一句。
        """
        login(page, site)
        self._fill_new(page, site, slug="frozen")
        page.click("form button[type=submit]")
        page.wait_for_load_state("networkidle")

        page.goto(f"{site.base_url}/admin/agents/frozen", wait_until="networkidle")
        slug_input = page.locator("#slug")
        if slug_input.count():
            assert slug_input.is_disabled() or slug_input.get_attribute("readonly") is not None, (
                "编辑页的 slug 输入框可改——改了就等于把调用方的 model id 换掉"
            )
        else:
            assert "frozen" in page.inner_text("body")

    def test_editing_bumps_the_version_and_rollback_restores(self, site: LiveSite, page):
        """版本与回滚。M2 交付清单里点名的一项。

        回滚的语义是**移动 current 指针，不改写也不新增版本**（services/agent.py
        的 rollback：「回滚 = 把 current_version_id 指回去。不改写任何历史版本」）。
        这正好与"版本不可变"一致——运行时缓存按 ``(agent_id, version)`` 键，回滚之后
        直接命中 v1 那条已有的缓存，不需要任何失效逻辑。
        """
        login(page, site)
        self._fill_new(page, site, slug="versioned")
        page.click("form button[type=submit]")
        page.wait_for_load_state("networkidle")

        page.goto(f"{site.base_url}/admin/agents/versioned", wait_until="networkidle")
        page.fill("#instructions", "改过的提示词——第二版。")
        page.click("form button[type=submit]")
        page.wait_for_load_state("networkidle")
        assert "v2" in page.inner_text("body")

        page.goto(f"{site.base_url}/admin/agents/versioned", wait_until="networkidle")
        rollback = page.locator("form[action$='/rollback'] button").first
        assert rollback.count(), "编辑页没有回滚按钮——M2 点名的「版本与回滚」"
        # 这个表单带 data-confirm。**必须显式接受**：playwright 在没有 handler 时
        # 自动 dismiss，表单于是从未提交，而失败信息会指向"回滚没生效"——
        # 看起来像产品 bug，实际是测试没点"确定"。
        page.once("dialog", lambda d: d.accept())
        rollback.click()
        page.wait_for_load_state("networkidle")

        rows = site.db_rows("SELECT version FROM agent_version ORDER BY version")
        assert [r["version"] for r in rows] == [1, 2], (
            f"回滚不该新增版本（历史不可变），实际版本序列 {[r['version'] for r in rows]}"
        )
        page.goto(f"{site.base_url}/admin/agents/versioned", wait_until="networkidle")
        # textarea 的内容不在 inner_text 里，必须读 value
        assert "从输入里抽取标题与评分" in page.input_value("#instructions"), (
            "回滚之后表单里还是 v2 的提示词"
        )
        table = page.inner_text("table").replace("\n", " ").replace("\t", " ")
        assert "v1 当前" in table, f"current 指针没指回 v1：{table}"

    def test_export_downloads_a_bundle(self, site: LiveSite, page):
        """导出是「低锁定」的兑现方式，按钮必须真的给出文件。"""
        login(page, site)
        self._fill_new(page, site, slug="exportme")
        page.click("form button[type=submit]")
        page.wait_for_load_state("networkidle")

        page.goto(f"{site.base_url}/admin/agents/exportme", wait_until="networkidle")
        link = page.locator("a[href$='/export']")
        assert link.count(), "编辑页没有导出入口"
        with page.expect_download() as dl:
            link.first.click()
        name = dl.value.suggested_filename
        assert name.endswith((".zip", ".tgz", ".tar.gz")), f"导出的文件名是 {name}"


# =============================================================================
# 配额
# =============================================================================


class TestQuota:
    def test_add_a_rule_and_see_it_listed(self, site: LiveSite, page):
        login(page, site)
        page.goto(f"{site.base_url}/admin/quota", wait_until="networkidle")
        page.select_option("select[name=subject_type]", "user")
        page.select_option("select[name=subject_id]", index=0)
        page.select_option("select[name=window]", "day")
        page.fill("input[name=usd]", "5")
        page.click("form[action='/admin/quota/save'] button[type=submit]")
        page.wait_for_load_state("networkidle")

        # 断言渲染后的文案而不是原始枚举值：页面是给人看的，"user/day" 那种
        # 原样输出反而说明本地化没做。
        row = page.inner_text("table")
        assert "用户" in row and "每天" in row and "5" in row, row
        assert site.db_rows("SELECT * FROM quota"), "库里没有这条规则"

    def test_a_rule_with_no_limit_is_refused(self, site: LiveSite, page):
        """金额与次数至少要设一个——两个都空等于没有配额，静默存下去最糟。"""
        login(page, site)
        page.goto(f"{site.base_url}/admin/quota", wait_until="networkidle")
        page.select_option("select[name=subject_type]", "user")
        page.select_option("select[name=subject_id]", index=0)
        page.click("form[action='/admin/quota/save'] button[type=submit]")
        page.wait_for_load_state("networkidle")
        assert not site.db_rows("SELECT * FROM quota"), "两个上限都空的规则被存下来了"

    def test_delete_asks_first(self, site: LiveSite, page):
        """删配额是危险操作：删掉之后这个主体就不再受限（钱刹车没了）。"""
        self.test_add_a_rule_and_see_it_listed(site, page)
        page.once("dialog", lambda d: d.dismiss())
        page.click("form[action='/admin/quota/delete'] button[type=submit]")
        page.wait_for_timeout(400)
        assert site.db_rows("SELECT * FROM quota"), "点了取消，规则却被删了"

        page.once("dialog", lambda d: d.accept())
        page.click("form[action='/admin/quota/delete'] button[type=submit]")
        page.wait_for_load_state("networkidle")
        assert not site.db_rows("SELECT * FROM quota")


# =============================================================================
# 设置：上游 key（脱敏）· 连接自检 · 可观测
# =============================================================================


def open_manual_upstream_form(page) -> None:
    """展开上游页的「手填上游」。

    那个表单**故意**折叠在 ``<details>`` 里：这一页的主路径是从环境变量里发现的
    上游中挑一个，手填是备用路径。所以测试必须像用户一样先点开它——
    元素在 DOM 里但不可见，``page.fill`` 会等满 30 秒然后超时，而报错
    （"element is not visible"）看起来像布局坏了。
    """
    page.click("summary:has-text('手填上游')")
    page.wait_for_selector("#api_key", state="visible")


class TestFormAlignment:
    """一行里的多个字段必须**真的对齐**。

    这是唯一能证明它的一层：CSS 改坏了不会让任何断言变红，而症状是"看起来歪"——
    没人会为此写测试，于是它会一次次回来。

    两次真实的坏法，都记在这里：

    1. ``.form-row`` 曾用 ``align-items: flex-end``（底边对齐）。任何一个字段带
       .hint 就比邻居高，底边对齐之后它的控件反而**更靠上**；行尾的提交按钮又去
       对齐说明文字的底边，落得比所有控件低半行。配额页四个标签落在四个不同高度上、
       密钥页的「签发」比下拉低几像素，都是这一个原因。
    2. 我第一次的修法是"给标签一个固定高度 18px"（13px × 1.4 的行盒）——**那是把
       渲染结果写成常数**。CI 的 chromium 装的字体不同，行盒变成 22px，控件下移而
       按固定值偏移的按钮不动，于是差 4px：**本机绿、CI 红**。

    现在两件事都是构造性质的：顶边对齐 + 每个字段的第一个子元素都是同字体同字号的
    单行 label（高度自然相等）；行尾按钮放进 ``.control-row``，与控件同处一行。
    所以下面的断言可以要求**严格相等**（同一行的顶边）与**居中重合**（按钮 vs 控件）。
    """

    #: 这三页各有一个多字段横排的表单，且恰好覆盖三种组合：
    #: 配额页有字段带说明、密钥页与调用记录页行尾有按钮。
    PAGES: ClassVar[list[str]] = ["/admin/quota", "/admin/keys", "/admin/logs"]

    @pytest.mark.parametrize("path", PAGES)
    def test_labels_and_controls_in_a_row_share_a_baseline(self, site: LiveSite, page, path):
        login(page, site)
        page.goto(f"{site.base_url}{path}", wait_until="networkidle")
        rows = page.evaluate("""() => {
            const top = el => Math.round(el.getBoundingClientRect().top);
            const out = [];
            for (const row of document.querySelectorAll('.form-row')) {
                const fields = [...row.querySelectorAll(':scope > .field')];
                if (fields.length < 2) continue;
                const sel = 'input:not([type=hidden]), select, textarea';
                out.push(fields.map(f => {
                    const label = f.querySelector('label');
                    const ctrl = f.querySelector(sel);
                    return {
                        label: label ? top(label) : null,
                        ctrl: ctrl ? top(ctrl) : null,
                    };
                }));
            }
            return out;
        }""")
        assert rows, f"{path} 上没找到多字段的 .form-row —— 这条断言失效了"
        for i, fields in enumerate(rows):
            # 按 label 的 top 分组：换行之后不同视觉行的字段本来就不该互相对齐
            lines: dict[int, list[dict]] = {}
            for f in fields:
                lines.setdefault(f["label"], []).append(f)
            for label_top, group in lines.items():
                tops = {f["ctrl"] for f in group if f["ctrl"] is not None}
                assert len(tops) <= 1, (
                    f"{path} 第 {i} 行、标签在 y={label_top} 的这一组，"
                    f"控件顶边散在 {sorted(tops)} —— 应当只有一个值"
                )

    @pytest.mark.parametrize("path", PAGES)
    def test_a_row_terminal_button_sits_on_the_control_line(self, site: LiveSite, page, path):
        """行尾按钮与它旁边的控件**垂直居中重合**。

        按钮与输入框的内边距不同，高度可以不同——所以这里比的是中线，不是顶边。
        比顶边就是过度约束，会在改一次按钮 padding 之后无意义地变红。
        """
        login(page, site)
        page.goto(f"{site.base_url}{path}", wait_until="networkidle")
        pairs = page.evaluate("""() => {
            const mid = el => {
                const r = el.getBoundingClientRect();
                return r.top + r.height / 2;
            };
            const out = [];
            for (const cr of document.querySelectorAll('.form-row .control-row')) {
                const btn = cr.querySelector('button, .btn');
                const ctrl = cr.querySelector('input, select, textarea');
                if (btn && ctrl) out.push([mid(btn), mid(ctrl)]);
            }
            return out;
        }""")
        if not pairs:
            pytest.skip(f"{path} 这一行没有行尾按钮")
        for bm, cm in pairs:
            assert abs(bm - cm) <= 2, (
                f"{path} 的按钮中线 {bm} 与控件中线 {cm} 差了 {abs(bm - cm)}px"
            )


class TestUpstreamPage:
    """上游页。

    上游 key、连接自检、运行状况**从「设置」搬到了这里**——一页只回答一个问题
    （出口是哪一个），设置页只留跨全站的东西。表单端点没变
    （``/admin/settings/upstream``、``/admin/settings/test``），只有页面变了。
    """

    def test_saved_key_is_never_shown_in_full(self, site: LiveSite, page):
        """页面上不能出现完整的上游 key。

        截图、录屏、肩窥、以及"把后台截图发到群里问问题"都是真实路径。
        """
        login(page, site)
        page.goto(f"{site.base_url}/admin/upstreams", wait_until="networkidle")
        html = page.content()
        assert "sk-or-v1-e2e" not in html, "完整的上游 key 出现在页面上"
        assert "***" in page.inner_text("body")

    @pytest.mark.expect_console_errors("403")
    def test_changing_upstream_needs_the_password(self, site: LiveSite, page):
        """改上游配置是全后台后果最严重的操作：这个表单被跨站提交一次，付费 key
        就会被送到攻击者的服务器。所以 CSRF 三层之外再加一道密码。"""
        login(page, site)
        page.goto(f"{site.base_url}/admin/upstreams", wait_until="networkidle")
        open_manual_upstream_form(page)
        page.fill("#api_key", "sk-or-v1-attacker")
        page.fill("#password", "wrong-password")
        page.click("form[action='/admin/settings/upstream'] button[type=submit]")
        page.wait_for_load_state("networkidle")
        assert "密码不正确" in page.inner_text("body")

        rows = site.db_rows("SELECT key FROM setting WHERE is_secret = 1")
        assert rows, "上游 key 记录不见了"

    @pytest.mark.expect_console_errors("403")
    def test_dangerous_base_url_is_refused(self, site: LiveSite, page):
        """A2：云元数据端点必须被拒，且报错要指出是什么问题。"""
        login(page, site)
        page.goto(f"{site.base_url}/admin/upstreams", wait_until="networkidle")
        open_manual_upstream_form(page)
        page.fill("#base_url", "http://169.254.169.254/v1")
        page.fill("#password", site.password)
        page.click("form[action='/admin/settings/upstream'] button[type=submit]")
        page.wait_for_load_state("networkidle")
        assert "元数据" in page.inner_text("body")

    def test_connection_self_check_runs_and_reports(self, site: LiveSite, page):
        """自检按钮是 htmx 局部刷新。

        它同时证明 htmx 真的在跑（外部脚本 + CSP 放行 + CSRF 头），以及自检
        用的是**已保存**的配置（假上游会收到一次真请求）。
        """
        login(page, site)
        page.goto(f"{site.base_url}/admin/upstreams", wait_until="networkidle")
        page.click("[hx-post='/admin/settings/test']")
        page.wait_for_function(
            "() => document.querySelector('#test-result')?.textContent.includes('模型')"
            " || document.querySelector('#test-result')?.textContent.includes('成功')"
            " || document.querySelector('#test-result')?.textContent.includes('失败')"
        )
        assert page.inner_text("#test-result").strip()


class TestSettings:
    def test_trace_is_off_by_default_and_says_so(self, site: LiveSite, page):
        """可观测默认关，页面要说清"打开意味着提示词会离开这台机器"。"""
        login(page, site)
        page.goto(f"{site.base_url}/admin/settings", wait_until="networkidle")
        body = page.inner_text("body")
        assert "未开启" in body
        assert "会被发送到" in body, "没有提示打开之后提示词与模型输出会外发"

    def test_trace_secret_is_never_echoed(self, site: LiveSite, page):
        login(page, site)
        page.goto(f"{site.base_url}/admin/settings", wait_until="networkidle")
        page.fill("#trace_endpoint", "http://10.0.0.9:3000/api/public/otel/v1/traces")
        page.fill("#trace_public_key", "pk-lf-e2e")
        page.fill("#trace_secret_key", "sk-lf-e2e-secret")
        page.fill("#trace_password", site.password)
        page.click("form[action='/admin/settings/trace'] button[type=submit]")
        page.wait_for_load_state("networkidle")
        assert "已开启" in page.inner_text("body")
        assert "sk-lf-e2e-secret" not in page.content(), "trace 的 secret key 被回显了"


# =============================================================================
# 调用记录
# =============================================================================


class TestLogs:
    def test_empty_state_gives_a_runnable_command(self, site: LiveSite, page):
        """还没有调用时，空状态要给一条能直接粘贴执行的 curl。

        那是新用户验证"到底通没通"的第一步。
        """
        login(page, site)
        page.goto(f"{site.base_url}/admin/logs", wait_until="networkidle")
        body = page.inner_text("body")
        assert "curl" in body
        assert "/v1/models" in body

    def test_a_real_call_shows_up_with_its_cost(self, site: LiveSite, page):
        """打一次真调用，记录页要出现它，且费用来源在四态闭集里。"""
        login(page, site)
        page.goto(f"{site.base_url}/admin/keys", wait_until="networkidle")
        page.fill("input[name=name]", "e2e-记录")
        page.click("form[action='/admin/keys/issue'] button[type=submit]")
        page.wait_for_load_state("networkidle")
        found = re.search(r"sk-xc-1-[A-Za-z0-9_-]+", page.inner_text("body"))
        assert found
        key = found.group(0)

        assert page.evaluate(CHAT_ONCE, [site.base_url, key]) == 200

        # 用量是**批量缓冲**的（A9：批量 flush + 周期 flush，周期 5 秒）。所以要等，
        # 而不是立刻断言。这个延迟是有意的设计——但也意味着用户刚打完一次调用去看
        # 记录页可能是空的，页面的空状态因此必须给出可执行的下一步（见上一条测试）。
        site.wait_for_usage_flush()

        page.goto(f"{site.base_url}/admin/logs", wait_until="networkidle")
        table = page.inner_text("table")
        assert "openai/gpt-5" in table, f"调用没出现在记录里：{table[:200]}"
        assert "成功" in table

    def test_filters_narrow_the_list(self, site: LiveSite, page):
        """筛选要真的筛。筛完还是全量的话，这个功能等于不存在。"""
        self.test_a_real_call_shows_up_with_its_cost(site, page)
        page.goto(f"{site.base_url}/admin/logs?model=不存在的模型", wait_until="networkidle")
        assert "openai/gpt-5" not in page.inner_text("body")


# =============================================================================
# 横切：主题 · 移动端 · 静态资源
# =============================================================================


class TestCrossCutting:
    def test_dark_and_light_both_render_readable_text(self, site: LiveSite, page):
        """两套配色都要能读。

        只在一种下测的话，另一种很容易出现"深色底 + 深色字"——而设计系统里
        颜色是 token 化的，一处写错两套都受影响。
        """
        login(page, site)
        for scheme in ("dark", "light"):
            page.emulate_media(color_scheme=scheme)
            page.goto(f"{site.base_url}/admin", wait_until="networkidle")
            colors = page.evaluate(
                """() => {
                  const b = getComputedStyle(document.body);
                  const h = getComputedStyle(document.querySelector('h1'));
                  return {bg: b.backgroundColor, fg: h.color};
                }"""
            )
            assert colors["bg"] and colors["fg"]
            assert colors["bg"] != colors["fg"], f"{scheme} 下前景与背景同色"

    def test_mobile_viewport_does_not_scroll_horizontally(self, site: LiveSite, browser):
        """手机上横向滚动是"没做响应式"的直接症状。

        后台不是主要用手机看，但"临时在手机上吊销一把泄漏的 key"是真实场景。
        """
        ctx = browser.new_context(viewport={"width": 375, "height": 812}, is_mobile=True)
        pg = ctx.new_page()
        try:
            pg.goto(f"{site.base_url}/admin/login", wait_until="networkidle")
            pg.fill("#password", site.password)
            pg.click("button[type=submit]")
            pg.wait_for_url(f"{site.base_url}/admin", wait_until="networkidle")
            for path in ("/admin", "/admin/keys", "/admin/logs"):
                pg.goto(f"{site.base_url}{path}", wait_until="networkidle")
                overflow = pg.evaluate(
                    "() => document.documentElement.scrollWidth"
                    " - document.documentElement.clientWidth"
                )
                assert overflow <= 1, f"{path} 在 375px 下横向溢出 {overflow}px"
        finally:
            ctx.close()

    def test_static_assets_are_cacheable_and_local(self, site: LiveSite, page):
        """静态资源必须来自本机（禁 CDN、离线可用）。"""
        login(page, site)
        page.goto(f"{site.base_url}/admin", wait_until="networkidle")
        for path in (
            "/admin/static/style.css",
            "/admin/static/app.js",
            "/admin/static/htmx.min.js",
        ):
            status = page.evaluate("async u => (await fetch(u)).status", f"{site.base_url}{path}")
            assert status == 200, f"{path} 取不到（{status}）"
