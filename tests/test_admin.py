"""管理后台的安全性。

后台暴露在公网上，里面有一个能改写上游 base_url 的表单。**A1 与 A2 组合起来就是
一次点击盗走付费 key**：让管理员的浏览器 POST 一次把 base_url 指向攻击者，
下一次调用就把 key 送上门。所以这里的每一条都不是"锦上添花"。

注意 ``base_url="https://testserver"``：会话 cookie 是 ``Secure`` 的，用 http 测
客户端根本不会回传它，测出来的"通过"是假的。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient

from conftest import FakeUpstream
from xingcha import contract as C
from xingcha.app import create_app
from xingcha.config import Settings
from xingcha.core.urlguard import UnsafeUpstreamURL, check_upstream_url

PASSWORD = "a-long-enough-password"


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings), base_url="https://testserver") as c:
        yield c


@pytest.fixture
def logged_in(client: TestClient) -> TestClient:
    """完成首次设密并登录。"""
    r = client.post(
        "/admin/login",
        data={"password": PASSWORD, "confirm": PASSWORD},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    assert client.cookies.get("xc_session"), "登录后没有下发会话 cookie"
    return client


def csrf_of(client: TestClient) -> str:
    return client.cookies.get("xc_csrf") or ""


# =============================================================================
# 首次设密
# =============================================================================


class TestSetup:
    def test_admin_is_locked_until_password_is_set(self, client: TestClient):
        """未设密时后台不能是敞开的。

        首次部署到设密之间存在一个窗口，那个窗口里后台如果无需凭证就能进，
        等于把设置页（含上游 key）交给任何扫到这台机器的人。
        """
        r = client.get("/admin", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/admin/login"

    def test_setup_wizard_is_shown_first(self, client: TestClient):
        assert "设置管理员密码" in client.get("/admin/login").text

    def test_short_password_rejected(self, client: TestClient):
        r = client.post("/admin/login", data={"password": "short", "confirm": "short"})
        assert "至少 12 位" in r.text

    def test_mismatched_confirmation_rejected(self, client: TestClient):
        r = client.post("/admin/login", data={"password": PASSWORD, "confirm": PASSWORD + "x"})
        assert "不一致" in r.text


# =============================================================================
# 会话 cookie 的属性 —— A1 的第一层
# =============================================================================


class TestSessionCookie:
    def test_cookie_flags(self, client: TestClient):
        r = client.post(
            "/admin/login",
            data={"password": PASSWORD, "confirm": PASSWORD},
            follow_redirects=False,
        )
        raw = "; ".join(
            v for k, v in r.headers.items() if k.lower() == "set-cookie" and "xc_session" in v
        )
        low = raw.lower()
        assert "httponly" in low, "会话 cookie 必须 HttpOnly —— 否则 XSS 能直接偷走它"
        assert "secure" in low, "会话 cookie 必须 Secure —— 否则会在明文链路上泄漏"
        assert "samesite=strict" in low, "SameSite=Strict 是 CSRF 的第一层"
        assert "path=/admin" in low, "作用域限定在 /admin，不要发给 /v1"

    def test_secure_follows_the_request_scheme(self, settings: Settings):
        """``Secure`` **跟随请求协议**，不写死。

        上面那条用的是 ``https://testserver``，验的是"HTTPS 下必须带 Secure"。
        这一条验反面：**纯 HTTP 部署下必须不带**。

        写死 True 的代价不是"更安全"，而是彻底不可用：浏览器直接丢掉 Secure cookie，
        用户看到"密码输对了却一直跳回登录页"，而服务端日志显示登录成功、会话已签发。
        两边都正常，是最难查的一类。（``localhost`` 例外——浏览器把它当安全上下文，
        所以本机开发看不出问题，只有换成局域网 IP 才炸。真踩过这个形状。）

        当前部署形态就是明文 HTTP（Caddy 已移除），所以这一条不是假设。
        """
        with TestClient(create_app(settings), base_url="http://testserver") as c:
            r = c.post(
                "/admin/login",
                data={"password": PASSWORD, "confirm": PASSWORD},
                follow_redirects=False,
            )
            assert r.status_code == 303, "HTTP 下也必须能登进去"
            cookies = [v for k, v in r.headers.items() if k.lower() == "set-cookie"]
            assert cookies, "一个 cookie 都没签发？"
            for raw in cookies:
                low = raw.lower()
                assert "secure" not in low, f"HTTP 下不该带 Secure：{raw}"
                # 丢掉 Secure **不等于**把其它防护一起丢掉
                assert "samesite=strict" in low, f"SameSite 不能跟着一起没了：{raw}"

    def test_logout_clears_session(self, logged_in: TestClient):
        assert logged_in.get("/admin", follow_redirects=False).status_code == 200
        logged_in.get("/admin/logout", follow_redirects=False)
        assert logged_in.get("/admin", follow_redirects=False).status_code == 303


# =============================================================================
# CSRF —— A1
# =============================================================================


class TestCSRF:
    def test_mutation_without_token_is_rejected(self, logged_in: TestClient):
        """没有 token 就不能改状态。哪怕已经登录。"""
        r = logged_in.post("/admin/keys/issue", data={"name": "x"}, follow_redirects=False)
        assert r.status_code == 403
        assert "CSRF" in r.text

    def test_mutation_with_wrong_token_is_rejected(self, logged_in: TestClient):
        r = logged_in.post(
            "/admin/keys/issue",
            data={"name": "x", "csrf_token": "not-the-right-token"},
            follow_redirects=False,
        )
        assert r.status_code == 403

    def test_mutation_with_correct_token_passes(self, logged_in: TestClient):
        logged_in.get("/admin/keys")  # 拿 csrf cookie
        r = logged_in.post(
            "/admin/keys/issue",
            data={"name": "ci", "csrf_token": csrf_of(logged_in)},
            follow_redirects=False,
        )
        assert r.status_code == 303

    def test_cross_site_request_is_rejected(self, logged_in: TestClient):
        """Sec-Fetch-Site 是现代浏览器一定会带且不可被脚本伪造的。

        这一层挡住的正是"攻击者页面上的表单自动提交"这种最常见的 CSRF 形态。
        """
        logged_in.get("/admin/keys")
        r = logged_in.post(
            "/admin/keys/issue",
            data={"name": "x", "csrf_token": csrf_of(logged_in)},
            headers={"Sec-Fetch-Site": "cross-site"},
            follow_redirects=False,
        )
        assert r.status_code == 403
        assert "跨站" in r.text

    def test_mismatched_origin_is_rejected(self, logged_in: TestClient):
        logged_in.get("/admin/keys")
        r = logged_in.post(
            "/admin/keys/issue",
            data={"name": "x", "csrf_token": csrf_of(logged_in)},
            headers={"Origin": "https://evil.example"},
            follow_redirects=False,
        )
        assert r.status_code == 403


class TestClickjacking:
    def test_frame_ancestors_none(self, client: TestClient):
        """否则攻击者能把后台套进透明 iframe，诱导管理员"点一下"，
        绕到与 CSRF 相同的结果。"""
        h = client.get("/admin/login").headers
        assert "frame-ancestors 'none'" in h["content-security-policy"]
        assert h["x-frame-options"] == "DENY"

    def test_csp_forbids_inline_and_external_scripts(self, client: TestClient):
        csp = client.get("/admin/login").headers["content-security-policy"]
        assert "script-src 'self'" in csp
        assert "unsafe-inline" not in csp.split("style-src")[0]


class TestPagesActuallyObeyTheirOwnCsp:
    """**光守住 CSP 头是不够的——还得有人拿页面去对那个头。**

    v0.4 之前恰恰是这样：上面那条测试断言了 `script-src 'self'` 无 unsafe-inline，
    而 base.html 里放着内联 <script>、keys.html 与 quota.html 用 `onsubmit=
    "return confirm(...)"`。浏览器把它们全部拒绝执行，于是

    - 「复制」按钮完全无反应，而同一个页面写着「这是唯一一次看到明文」；
    - 吊销密钥与删配额的 confirm() **根本不弹**，表单直接提交——
      「危险操作二次确认」在生产里不存在，而界面看起来像是有保护。

    真浏览器实测过：控制台每页都在报
    `Executing inline script violates ... The action has been blocked.`

    所以这一组是从**页面**这一侧断言的。
    """

    #: 内联事件处理器属性。CSP 无 unsafe-inline 时它们一律不执行。
    #:
    #: 危险的地方在于失败是**静默**的：onsubmit="return confirm(...)" 被挡掉之后
    #: 表单照常提交，看不出任何异常。
    INLINE_HANDLER = re.compile(r"\bon(?:submit|click|change|input|load|error)\s*=")

    def _templates(self) -> list[Path]:
        root = Path(__file__).resolve().parent.parent / "src" / "xingcha" / "web" / "templates"
        return sorted(root.rglob("*.html"))

    def test_no_template_has_an_inline_script_block(self):
        """``<script>`` 只允许带 src。带内容的一律不执行。"""
        bad = []
        for path in self._templates():
            for m in re.finditer(r"<script([^>]*)>(.*?)</script>", path.read_text("utf-8"), re.S):
                attrs, body = m.group(1), m.group(2).strip()
                if body and "src=" not in attrs:
                    bad.append(f"{path.name}: {body[:60]!r}")
        assert not bad, f"内联脚本会被 CSP 挡掉（改放 static/*.js）：{bad}"

    def test_no_template_uses_an_inline_event_handler(self):
        """``onsubmit=`` 一类改用 data-* 属性 + static/app.js 里的事件委托。"""
        bad = [
            f"{p.name}:{p.read_text('utf-8')[: m.start()].count(chr(10)) + 1}"
            for p in self._templates()
            for m in [self.INLINE_HANDLER.search(p.read_text("utf-8"))]
            if m
        ]
        assert not bad, f"内联事件处理器会被 CSP 静默挡掉：{bad}"

    def test_every_script_src_is_served_by_us(self, client: TestClient):
        """页面引用的每个脚本都必须真的能取到。

        `script-src 'self'` 也挡 CDN——这条顺带守住「禁 CDN、离线可用」那条硬要求。
        """
        html = client.get("/admin/login").text
        srcs = re.findall(r'<script[^>]*\bsrc="([^"]+)"', html)
        assert srcs, "页面一个脚本都没引用？复制按钮与二次确认都要靠它"
        for src in srcs:
            assert src.startswith("/admin/static/"), f"{src} 不是自己的路径，会被 CSP 挡掉"
            assert client.get(src).status_code == 200, f"{src} 取不到"

    def test_the_copy_button_has_a_handler_and_a_fallback(self, client: TestClient):
        """复制按钮与二次确认所依赖的钩子必须在 app.js 里真的存在。

        模板改了 data-* 名字而 app.js 没跟上的话，症状又是"点了没反应"——
        与内联脚本被挡掉一模一样，而且同样不报错。
        """
        js = client.get("/admin/static/app.js").text
        assert "data-copy" in js
        assert "form[data-confirm]" in js
        # http 下 navigator.clipboard 不可用，必须有回落
        assert "getSelection" in js, "剪贴板不可用时要能回落到选中文本"


# =============================================================================
# SSRF —— A2
# =============================================================================


class TestUpstreamURLGuard:
    @pytest.mark.parametrize(
        "url",
        [
            "http://169.254.169.254/latest/meta-data/",  # 云元数据：拿实例凭证
            "https://169.254.169.254/",
            "http://10.0.0.5/v1",
            "https://192.168.1.1/v1",
            "https://172.16.0.1/v1",
            "http://evil.example/v1",  # 非本机必须 https
            "file:///etc/passwd",
            "gopher://x/",
            "",
        ],
    )
    def test_rejects_dangerous(self, url: str):
        with pytest.raises(UnsafeUpstreamURL):
            check_upstream_url(url)

    def test_allows_public_https(self):
        checked = check_upstream_url("https://openrouter.ai/api/v1")
        assert checked.host == "openrouter.ai"
        assert checked.addresses

    def test_allows_explicit_loopback(self):
        """同机跑一个中转是合法用法，但必须是显式的回环主机名。"""
        assert check_upstream_url("http://127.0.0.1:3000/v1").host == "127.0.0.1"

    def test_trailing_slash_normalized(self):
        assert check_upstream_url("https://openrouter.ai/api/v1/").url.endswith("/api/v1")

    def test_error_says_why(self):
        """管理员看到的必须是原因，不是"失败了"——否则只会以为工具坏了。"""
        with pytest.raises(UnsafeUpstreamURL) as e:
            check_upstream_url("https://169.254.169.254/")
        assert "元数据" in str(e.value)


class TestGuardPolicySplit:
    """两处地址的守卫**故意不一样**，且都有理由。

    上游地址会带着付费 key 去打 → 内网一律拒（否则这台机器就是一个带凭证的内网
    探针）。trace 上报地址不带 key，风险是内容外流 → 允许内网（自建 Langfuse 就在
    那儿）。**链路本地两处都拒**——那是真正危险的目标。
    """

    def test_upstream_rejects_private_networks(self):
        with pytest.raises(UnsafeUpstreamURL) as e:
            check_upstream_url("http://10.0.0.5/v1")
        assert "私有网段" in str(e.value)

    def test_trace_allows_private_networks(self):
        checked = check_upstream_url("http://10.0.0.5:3000/x", allow_private=True)
        assert checked.host == "10.0.0.5"

    def test_link_local_is_blocked_in_both(self):
        for kwargs in ({}, {"allow_private": True}):
            with pytest.raises(UnsafeUpstreamURL) as e:
                check_upstream_url("http://169.254.169.254/x", **kwargs)  # type: ignore[arg-type]
            assert "元数据" in str(e.value)

    def test_public_http_is_still_rejected_even_for_trace(self):
        """放开的只有"内网可以用 http"，公网仍然必须 https。

        不然一个填错的 http://cloud.langfuse.com 会让整份对话内容在链路上明文走。
        """
        with pytest.raises(UnsafeUpstreamURL) as e:
            check_upstream_url("http://1.1.1.1/x", allow_private=True)
        assert "https" in str(e.value)


class TestAddProvider:
    """「添加供应商」：手填一个上游，**只进列表，不切换**。

    加和用是两件事：加进来是"我以后可能用它"，切过去是"现在就换出口"，而后者会打断
    所有现有 Agent。绑在一个按钮上等于每次新增供应商都强制来一次出口变更。

    与此前的"手填上游"的实质区别是**它会被记住**。旧行为直接覆盖当前出口、不留
    痕迹——切走之后就只能把 key 再输一遍，也就是说"手填"事实上是一条单向门。
    """

    def _post(self, client: TestClient, **data):
        client.get("/admin/upstreams")
        return client.post(
            "/admin/upstreams/providers",
            data={"csrf_token": csrf_of(client), **data},
            follow_redirects=False,
        )

    @staticmethod
    def _err(r) -> str:
        m = re.search(r"alert danger[^>]*><div>([^<]*)", r.text)
        return m.group(1) if m else "（无 alert）"

    def test_requires_password_confirmation(self, logged_in: TestClient):
        """改上游是全后台后果最严重的操作，CSRF 三层之外再加一道。"""
        r = self._post(
            logged_in,
            password="wrong-password",
            name="坏人",
            base_url="https://evil.example.com/v1",
            api_key="sk-attacker",
        )
        assert r.status_code == 200
        assert "当前密码不正确" in r.text
        assert "evil.example.com" not in logged_in.get("/admin/upstreams").text

    def test_the_password_check_works_when_it_comes_from_the_environment(
        self, settings: Settings, upstream: FakeUpstream
    ):
        """**密码由环境变量托管时也要能通过。**

        此前这里用的是 ``verify_password``（只查库里的 argon2id 哈希），而环境变量
        托管时库里根本没有哈希 —— 于是它对任何输入都返回 False：用户输的是对的，
        却永远被告知"密码不正确"，日志里也什么都没有。整条"改上游"的路被堵死，
        而看起来像是密码记错了。实际撞过。
        """
        env_pw = "env-managed-password-1"
        settings.admin_password = env_pw
        with TestClient(create_app(settings), base_url="https://testserver") as c:
            assert (
                c.post(
                    "/admin/login", data={"password": env_pw}, follow_redirects=False
                ).status_code
                == 303
            )
            c.get("/admin/upstreams")
            r = c.post(
                "/admin/upstreams/providers",
                data={
                    "csrf_token": csrf_of(c),
                    "password": env_pw,
                    "name": "自建中转",
                    "base_url": upstream.base_url,
                    "api_key": "sk-relay-000",
                },
                follow_redirects=False,
            )
            assert r.status_code == 204, f"环境变量密码被拒了：{r.text[:300]}"

    def test_an_unreachable_upstream_saves_nothing_at_all(self, logged_in: TestClient):
        """**拉不通就不保存。**

        此前是"先存下来再探测"，理由是"失败时用户填的东西不要白丢"。那个理由错在两处：
        失败的条目会留在列表里（用户看到一个从来没通过的条目，还得自己去删），
        而"不丢输入"根本不该靠落库实现——表单走 htmx，失败时页面不重载，输入本来就在。

        用户报的就是这个：地址打错成 /v111，点保存之后「目前使用」被改成了那条坏配置。
        """
        before = logged_in.get("/admin/upstreams").text
        r = self._post(
            logged_in,
            password=PASSWORD,
            name="打不通的",
            # 本机上没人监听的端口：SSRF 守卫放行 localhost，但连不上
            base_url="http://127.0.0.1:9/v1",
            api_key="sk-nope",
        )
        assert r.status_code == 200
        assert "没有保存" in r.text

        after = logged_in.get("/admin/upstreams").text
        assert "打不通的" not in after, "拉不通却把它存进了列表"
        assert "127.0.0.1:9" not in after, "拉不通却把它写成了当前出口"
        # 出口一个字节都没动
        assert ("生效中" in before) == ("生效中" in after)

    def test_dangerous_base_url_is_rejected_even_with_password(self, logged_in: TestClient):
        r = self._post(
            logged_in,
            password=PASSWORD,
            name="元数据",
            base_url="http://169.254.169.254/",
            api_key="sk-x",
        )
        assert r.status_code == 200
        assert "元数据" in r.text

    def test_saving_does_not_change_the_active_upstream(
        self, logged_in: TestClient, upstream: FakeUpstream
    ):
        """**保存不等于切换。**

        点「保存」之后当前出口必须一个字节都没动——要用它得再去列表点
        「检查并切换」，那条路径会先列出哪些 Agent 会失效。
        """
        state = logged_in.app.state.xc  # type: ignore[attr-defined]
        before = state.upstream.config
        r = self._post(
            logged_in,
            password=PASSWORD,
            name="备用中转",
            base_url=upstream.base_url,
            api_key="sk-standby-000",
        )
        assert r.status_code == 204, self._err(r)
        assert state.upstream.config == before, "保存却把出口换了"
        assert "备用中转" in logged_in.get("/admin/upstreams").text

    def test_a_saved_provider_shows_up_in_the_switch_list(
        self, logged_in: TestClient, upstream: FakeUpstream
    ):
        """**这条是整个改动的理由。**

        加完之后它必须出现在切换列表里，否则切走了就回不来——那正是旧行为的问题。

        成功返回 **204 + HX-Redirect**，不是 303：这个表单走 htmx（失败时不重载页面，
        输入才留得住），而 htmx 会去跟随 303、把整页 HTML 塞进那个结果小方块里。
        """
        r = self._post(
            logged_in,
            password=PASSWORD,
            name="公司内网中转",
            base_url=upstream.base_url,
            api_key="sk-relay-legit-000",
        )
        assert r.status_code == 204, self._err(r)
        assert r.headers["hx-redirect"] == "/admin/upstreams"
        body = logged_in.get("/admin/upstreams").text
        assert "公司内网中转" in body
        assert "sk-relay-legit-000" not in body, "完整 key 出现在页面上"
        assert "***" in body

    def test_the_key_is_encrypted_at_rest(
        self, logged_in: TestClient, settings: Settings, upstream: FakeUpstream
    ):
        """供应商列表是个 JSON blob，里面装着每家的 key——必须整体加密落盘。

        漏了的话就是一串明文 key 躺在 SQLite 里，而页面上一切正常。
        """
        self.test_a_saved_provider_shows_up_in_the_switch_list(logged_in, upstream)
        raw = settings.db_path.read_bytes()
        assert b"sk-relay-legit-000" not in raw

    def test_deleting_a_provider_does_not_touch_the_active_upstream(
        self, logged_in: TestClient, upstream: FakeUpstream
    ):
        """删的是"列表里那一行"，不是"正在用的配置"。

        混在一起的话，删一行会让服务当场失去上游，而用户以为自己只是在整理列表。
        """
        self.test_a_saved_provider_shows_up_in_the_switch_list(logged_in, upstream)

        logged_in.get("/admin/upstreams")
        r = logged_in.post(
            "/admin/upstreams/providers/delete",
            data={"name": "公司内网中转", "csrf_token": csrf_of(logged_in)},
            follow_redirects=False,
        )
        assert r.status_code == 303
        after = logged_in.get("/admin/upstreams").text
        # 列表里那一行没了：删除按钮消失就是它不在列表里了
        assert 'value="公司内网中转"' not in after, "列表里那一行还在"

    def test_settings_page_no_longer_holds_the_upstream_form(self, logged_in: TestClient):
        """设置页**不该**再有上游表单——它整块搬走了。

        这条防的是"搬走了但忘了删"：两处都能改同一个值时，用户改了一处、另一处
        显示的还是旧的，而没人知道哪个是真的。
        """
        body = logged_in.get("/admin/settings").text
        assert 'action="/admin/settings/upstream"' not in body
        assert 'action="/admin/upstreams/providers"' not in body, "添加供应商表单不该在设置页"


class TestTraceMutation:
    """trace 配置与上游 key 同级。

    打开 trace 意味着提示词与模型输出会被送到一个外部地址。这个表单一旦被跨站
    提交，攻击者就得到了一份**持续到达**的对话副本——后果与偷走 key 是同一个量级，
    所以守卫也必须同级。
    """

    def _post(self, client: TestClient, **data):
        client.get("/admin/settings")
        return client.post(
            "/admin/settings/trace",
            data={"csrf_token": csrf_of(client), **data},
            follow_redirects=False,
        )

    def test_requires_password_confirmation(self, logged_in: TestClient):
        """密码不对 → **什么都不改**，并且就地回显这一页。

        断言的是"没改成任何东西 + 原因看得见"，不是状态码。此前是 403 + 一个独立的
        错误页；现在是 200 + 页面内的错误条。两者在安全上等价（写库前就拒了），
        但后者不会把用户填的地址与 key 一起丢掉——而"密码错"正是这个表单最常见的
        失败，它要填地址 + 两把 key + 密码。
        """
        r = self._post(
            logged_in,
            password="wrong-password",
            endpoint="http://10.0.0.5:3000/api/public/otel/v1/traces",
        )
        assert r.status_code == 200
        assert "当前密码不正确" in r.text
        assert 'action="/admin/settings/trace"' in r.text, "没有回到设置页，用户会丢掉已填内容"

        state = logged_in.app.state.xc  # type: ignore[attr-defined]
        assert state.tracing is None, "密码不对却把 trace 打开了"

    def test_a_rejected_attempt_keeps_what_was_typed_but_never_the_secret(
        self, logged_in: TestClient
    ):
        """回显已填的地址与 public key，**但绝不回显 secret key**。

        回显 secret 就是把它写进 HTML —— 那份 HTML 会进浏览器缓存、进截图、
        进"把后台截图发到群里问问题"。
        """
        r = self._post(
            logged_in,
            password="wrong-password",
            endpoint="http://10.0.0.5:3000/api/public/otel/v1/traces",
            public_key="pk-lf-keepme",
            secret_key="sk-lf-must-not-echo",
        )
        assert 'value="http://10.0.0.5:3000/api/public/otel/v1/traces"' in r.text
        assert 'value="pk-lf-keepme"' in r.text
        assert "sk-lf-must-not-echo" not in r.text

    def test_metadata_endpoint_is_still_blocked(self, logged_in: TestClient):
        """上报地址是"服务端会主动去打"的地址，云元数据端点一律拒。

        不守的话，这个表单就是一个"让星槎去打云元数据端点"的原语。
        """
        r = self._post(logged_in, password=PASSWORD, endpoint="http://169.254.169.254/v1/traces")
        assert r.status_code == 200
        assert "元数据" in r.text
        state = logged_in.app.state.xc  # type: ignore[attr-defined]
        assert state.tracing is None

    def test_a_self_hosted_langfuse_on_a_private_network_is_allowed(self, logged_in: TestClient):
        """**自建 Langfuse 基本就在内网**，这条不能被 SSRF 守卫挡死。

        挡死之后人们会去用托管服务——那正好是更差的隐私结果。上游地址的守卫更严
        （内网一律拒），因为那条链路会带着付费 key；trace 这条不带。
        """
        r = self._post(
            logged_in,
            password=PASSWORD,
            endpoint="http://172.20.0.9:3000/api/public/otel/v1/traces",
        )
        assert r.status_code == 303

    def test_valid_config_turns_tracing_on(self, logged_in: TestClient):
        r = self._post(
            logged_in,
            password=PASSWORD,
            # 自建 Langfuse 的典型形态：内网 http。这条必须被**放过**。
            endpoint="http://10.0.0.5:3000/api/public/otel/v1/traces",
            public_key="pk-lf-1",
            secret_key="sk-lf-2",
        )
        assert r.status_code == 303
        body = logged_in.get("/admin/settings").text
        assert "上报中" in body
        assert "sk-lf-2" not in body, "secret key 绝不能回显到页面上"

    def test_an_endpoint_without_keys_is_enough(self, logged_in: TestClient):
        """**只填地址、不填 key 也要能用。**

        Langfuse 用 Basic 认证所以需要两把 key，而 OTel Collector / Jaeger 这类
        后端通常不校验身份。页面上照这个写的（"只有 Langfuse 需要这两把 key"），
        所以它必须真的成立——否则那句话就是错的引导。
        """
        r = self._post(logged_in, password=PASSWORD, endpoint="http://10.0.0.5:4318/v1/traces")
        assert r.status_code == 303
        state = logged_in.app.state.xc  # type: ignore[attr-defined]
        assert state.tracing is not None, "不填 key 就开不起来，而页面说可以"

    def _read(self, logged_in: TestClient, *keys):
        import asyncio

        from xingcha.services import setting as setting_svc

        state = logged_in.app.state.xc  # type: ignore[attr-defined]

        async def read():
            async with state.sessionmaker() as s:
                return [await setting_svc.get(s, state.keyring, k) for k in keys]

        return asyncio.run(read())

    def _toggle(self, client: TestClient):
        client.get("/admin/settings")
        return client.post(
            "/admin/settings/trace/toggle",
            data={"csrf_token": csrf_of(client)},
            follow_redirects=False,
        )

    def test_toggle_stops_reporting_without_losing_the_credentials(self, logged_in: TestClient):
        """**停用不该丢配置。**

        原先只有一个状态：地址非空即开启，清空即关闭——而清空会把两把 key 一起
        删掉。于是"先停一下上报"的代价是下次要把 Langfuse 凭据重新找出来贴一遍，
        人自然就不停了。不好停的开关等于一个默认开着的开关。
        """
        from xingcha import contract as C

        self.test_valid_config_turns_tracing_on(logged_in)
        state = logged_in.app.state.xc  # type: ignore[attr-defined]

        assert self._toggle(logged_in).status_code == 303
        assert state.tracing is None, "停用了却还在上报"

        endpoint, pk, sk = self._read(
            logged_in,
            C.SETTING_KEY_TRACE_ENDPOINT,
            C.SETTING_KEY_TRACE_PUBLIC_KEY,
            C.SETTING_KEY_TRACE_SECRET_KEY,
        )
        assert endpoint and pk and sk, "停用把配置也删了"

        body = logged_in.get("/admin/settings").text
        assert "已停用" in body, "页面看不出是自己关的还是没配过"

        assert self._toggle(logged_in).status_code == 303
        assert state.tracing is not None, "开不回来"

    def test_toggle_survives_a_restart(self, logged_in: TestClient):
        """开关存在库里，不是进程内的一个 flag。

        只存内存的话，重启之后上报会**自己恢复**——而人以为自己关掉了。
        """
        from xingcha import contract as C

        self.test_valid_config_turns_tracing_on(logged_in)
        self._toggle(logged_in)
        assert self._read(logged_in, C.SETTING_KEY_TRACE_ENABLED) == ["0"]

    def test_toggle_needs_no_endpoint_to_fail_gracefully(self, logged_in: TestClient):
        r = self._toggle(logged_in)
        assert r.status_code == 200
        assert "还没有配置上报地址" in r.text

    def test_clear_wipes_everything_but_needs_the_password(self, logged_in: TestClient):
        from xingcha import contract as C

        self.test_valid_config_turns_tracing_on(logged_in)
        logged_in.get("/admin/settings")
        r = logged_in.post(
            "/admin/settings/trace/clear",
            data={"csrf_token": csrf_of(logged_in), "password": "wrong-one"},
            follow_redirects=False,
        )
        assert r.status_code == 200
        assert self._read(logged_in, C.SETTING_KEY_TRACE_ENDPOINT) != [None]

        logged_in.get("/admin/settings")
        r = logged_in.post(
            "/admin/settings/trace/clear",
            data={"csrf_token": csrf_of(logged_in), "password": PASSWORD},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert self._read(
            logged_in,
            C.SETTING_KEY_TRACE_ENDPOINT,
            C.SETTING_KEY_TRACE_PUBLIC_KEY,
            C.SETTING_KEY_TRACE_SECRET_KEY,
        ) == [None, None, None]

    def test_clearing_the_endpoint_also_wipes_the_credentials(self, logged_in: TestClient):
        """关掉 trace 时凭据一起清掉。

        留着一份用不上的 secret key 只是多一处泄漏面——而"我以为已经关了"恰恰是
        这种残留最容易发生的场景。
        """
        self.test_valid_config_turns_tracing_on(logged_in)
        r = self._post(logged_in, password=PASSWORD, endpoint="")
        assert r.status_code == 303

        state = logged_in.app.state.xc  # type: ignore[attr-defined]
        assert state.tracing is None

        import asyncio

        from xingcha import contract as C
        from xingcha.services import setting as setting_svc

        async def read():
            async with state.sessionmaker() as s:
                return [
                    await setting_svc.get(s, state.keyring, k)
                    for k in (
                        C.SETTING_KEY_TRACE_ENDPOINT,
                        C.SETTING_KEY_TRACE_PUBLIC_KEY,
                        C.SETTING_KEY_TRACE_SECRET_KEY,
                    )
                ]

        assert asyncio.run(read()) == [None, None, None]


class TestPasswordFormErrors:
    """改密码的失败也要就地回显。

    这个表单三个字段全是密码，跳到独立错误页之后要全部重填——而"两次不一致"
    与"当前密码错"都是高频失误。
    """

    def _post(self, client: TestClient, **data):
        client.get("/admin/settings")
        return client.post(
            "/admin/settings/password",
            data={"csrf_token": csrf_of(client), **data},
            follow_redirects=False,
        )

    def test_mismatch_renders_inline(self, logged_in: TestClient):
        r = self._post(
            logged_in, current=PASSWORD, new_password="a-new-password-1", confirm="something-else-1"
        )
        assert r.status_code == 200
        assert "两次输入的新密码不一致" in r.text
        assert 'action="/admin/settings/password"' in r.text

    def test_wrong_current_password_renders_inline_and_changes_nothing(self, logged_in: TestClient):
        new = "a-new-password-1"
        r = self._post(logged_in, current="wrong-password", new_password=new, confirm=new)
        assert r.status_code == 200
        assert "当前密码不正确" in r.text
        # 旧密码仍然有效 —— 也就是真的什么都没改
        logged_in.get("/admin/logout")
        assert (
            logged_in.post(
                "/admin/login", data={"password": PASSWORD}, follow_redirects=False
            ).status_code
            == 303
        )

    def test_too_short_renders_inline(self, logged_in: TestClient):
        r = self._post(logged_in, current=PASSWORD, new_password="short", confirm="short")
        assert r.status_code == 200
        assert str(C.MIN_ADMIN_PASSWORD_LEN) in r.text


class TestSettingsCopy:
    """页面文案要**指导填写**，而不是记录实现过程。

    这一层不是风格洁癖：设置页曾经用整段话解释"上游 key 为什么搬去了上游页"、
    "运行列表回答不了什么问题"。那些句子对着屏幕的人一个决定都帮不上——他要知道的
    是这一格填什么、填错会怎样。而且它们会过期：读到的人无从判断"搬"是昨天还是
    半年前的事。
    """

    def test_no_changelog_prose(self, logged_in: TestClient):
        body = logged_in.get("/admin/settings").text
        for phrase in ("搬到了", "才说得通", "实现过程"):
            assert phrase not in body, f"设置页出现了叙述实现过程的文案：{phrase}"

    def test_the_otlp_hint_answers_how_to_fill_it_for_a_self_hosted_backend(
        self, logged_in: TestClient
    ):
        """自部署是主要场景，页面必须直接给出可照抄的形态。

        用户实际问过这一条：占位符只有 cloud.langfuse.com，自建的该怎么填。
        """
        body = logged_in.get("/admin/settings").text
        assert "/api/public/otel/v1/traces" in body, "没给 Langfuse 的 traces 路径"
        assert "4318" in body, "没给 OTLP/HTTP 的默认端口（Collector / Jaeger）"
        assert "OTLP/HTTP" in body, "没说清要填的是 OTLP 入口而不是后端的网页地址"

    def test_the_page_says_the_keys_are_langfuse_only(self, logged_in: TestClient):
        """与 load_tracing 的实际行为对齐：两把 key 都为空时不发 Basic 头。"""
        body = logged_in.get("/admin/settings").text
        assert "只有 Langfuse 需要这两把 key" in body

    def test_the_password_is_asked_for_in_a_dialog(self, logged_in: TestClient):
        """当前密码走**弹窗**，不占表单版面。

        表单上只留"要配什么"，身份确认是提交那一刻的事——与上游页的「添加供应商」
        一致。``<dialog>`` 在 ``<form>`` 内部，所以密码仍是这个表单的字段，
        提交时一起发出去，不需要 JS 搬运值。
        """
        body = logged_in.get("/admin/settings").text
        assert 'data-open-dialog="#trace-pw-dialog"' in body, "保存按钮没接上密码弹窗"
        assert '<dialog id="trace-pw-dialog"' in body
        # 密码框仍然在同一个 form 里（在 dialog 之内），否则提交时带不出去
        form = body[body.index('action="/admin/settings/trace"') :]
        form = form[: form.index("</form>")]
        assert 'id="trace_password"' in form, "密码框跑到 form 外面去了，提交带不出去"


class TestThemeSwitch:
    """三态主题：跟随系统 / 亮 / 暗。

    用 cookie 而不是 localStorage：**服务端渲染时就得知道选了哪个**，否则
    `data-theme` 要靠 JS 在首屏之后补，用户会看到一次闪白/闪黑。而内联 script 又被
    CSP（``script-src 'self'``）挡着，加不进 <head>。

    此前 `data-theme` 一直是空串、也没有任何切换入口——两套配色都写了，用户却选不了。
    """

    def _post(self, client: TestClient, **data):
        client.get("/admin")
        return client.post(
            "/admin/theme",
            data={"csrf_token": csrf_of(client), **data},
            follow_redirects=False,
        )

    @staticmethod
    def _attr(html: str) -> str:
        m = re.search(r'<html[^>]*data-theme="([^"]*)"', html)
        assert m, "根元素上没有 data-theme"
        return m.group(1)

    def test_default_follows_the_system(self, logged_in: TestClient):
        """默认不写 data-theme，交给 CSS 的 prefers-color-scheme。

        写死一个值就等于替用户的系统设置做决定，而那个决定在深夜会很刺眼。
        """
        assert self._attr(logged_in.get("/admin").text) == ""

    def test_switching_sticks_and_returns_to_where_you_were(self, logged_in: TestClient):
        r = self._post(logged_in, value="light", back="/admin/logs")
        assert r.status_code == 303
        assert r.headers["location"] == "/admin/logs"
        assert self._attr(logged_in.get("/admin/logs").text) == "light"

    def test_an_off_site_back_is_refused(self, logged_in: TestClient):
        """不校验 ``back`` 的话这就是一个**开放重定向**。

        ``/admin/theme`` 带上 ``back=https://坏人.com`` 就能把已登录的管理员送出去，
        而链接看起来完全是自己站里的。
        """
        for hostile in ("https://evil.example.com/", "//evil.example.com/", "/etc/passwd"):
            r = self._post(logged_in, value="dark", back=hostile)
            assert r.headers["location"] == "/admin", f"{hostile} 没被挡住"

    def test_an_unknown_value_is_refused(self, logged_in: TestClient):
        assert self._post(logged_in, value="neon").status_code == 403

    def test_a_hostile_cookie_never_reaches_the_attribute(self, logged_in: TestClient):
        """cookie 是用户可写的，不能直接塞进 HTML 属性。

        闭集之外的值一律当作"没设"——这样 ``xc_theme='"><script>`` 只是被忽略，
        而不是被写进 ``<html data-theme="...">``。
        """
        logged_in.cookies.set("xc_theme", '"><script>alert(1)</script>')
        body = logged_in.get("/admin").text
        assert self._attr(body) == ""
        assert "<script>alert(1)</script>" not in body

    def test_the_login_page_is_always_dark(self, client: TestClient):
        """**登录页不跟随主题。**

        银河是夜景——"亮色银河"是个矛盾：白底上的星点不像星，像页面没渲染干净。
        与其做一套注定难看的亮色，不如让这一页只有一种样子。
        """
        client.cookies.set("xc_theme", "light")
        assert self._attr(client.get("/admin/login").text) == "dark"

    def test_the_switch_is_actually_on_the_page(self, logged_in: TestClient):
        """两套配色写了却没有入口，等于没做。"""
        body = logged_in.get("/admin").text
        assert 'action="/admin/theme"' in body
        for value in C.THEMES:
            assert f'value="{value}"' in body, f"切换器缺 {value}"


# =============================================================================
# 密钥页
# =============================================================================


class TestKeysPage:
    def test_issue_shows_plaintext_once(self, logged_in: TestClient):
        logged_in.get("/admin/keys")
        r = logged_in.post(
            "/admin/keys/issue",
            data={"name": "本地开发", "csrf_token": csrf_of(logged_in)},
            follow_redirects=True,
        )
        assert "唯一一次看到明文" in r.text
        assert C.TOKEN_PREFIX in r.text

    def test_list_never_shows_plaintext(self, logged_in: TestClient):
        """列表页只显示不可推导的标识，不显示秘密本体的任何字符。"""
        logged_in.get("/admin/keys")
        logged_in.post(
            "/admin/keys/issue",
            data={"name": "x", "csrf_token": csrf_of(logged_in)},
            follow_redirects=True,
        )
        body = logged_in.get("/admin/keys").text  # 不带 issued 参数
        assert "唯一一次看到明文" not in body
        # display_prefix 形如 sk-xc-1-<kid>，长度固定 24
        import re

        for shown in re.findall(r"sk-xc-1-[0-9a-z]+", body):
            assert len(shown) == 24, f"页面上出现了超出 display_prefix 的内容：{shown}"


# =============================================================================
# 免鉴权面的闭集
# =============================================================================


def test_admin_unauthenticated_surface_is_minimal(settings: Settings, tmp_path: Path):
    """后台里只有登录页与静态资源可以免鉴权。

    新增一个免鉴权的后台页面必须让这条变红——那正是"顺手放出去一个管理端点"
    最容易发生的地方。
    """
    with TestClient(create_app(settings), base_url="https://testserver") as c:
        for path in ("/admin", "/admin/keys", "/admin/logs", "/admin/settings"):
            r = c.get(path, follow_redirects=False)
            assert r.status_code == 303, f"{path} 未登录时应当跳转登录页"
        assert c.get("/admin/login").status_code == 200
        assert c.get("/admin/static/style.css").status_code == 200


# =============================================================================
# Agent 表单
# =============================================================================


SCHEMA_TEXT = (
    '{"type":"object","properties":{"客户名称":{"type":"string","description":"甲方全称"}},'
    '"required":["客户名称"]}'
)


class TestAgentForm:
    def _create(self, client: TestClient, **over) -> httpx2.Response:
        client.get("/admin/agents/new")
        data = {
            "slug": "extract",
            "name": "抽取",
            "description": "把合同抽成字段",
            "instructions": "抽取甲方名称",
            "model": "openai/gpt-5",
            "output_schema": SCHEMA_TEXT,
            "tier": "T2",
            "retries": "2",
            "csrf_token": csrf_of(client),
        }
        data.update(over)
        return client.post("/admin/agents/save", data=data, follow_redirects=False)

    def test_create_and_list(self, logged_in: TestClient):
        r = self._create(logged_in)
        assert r.status_code == 303
        assert r.headers["location"] == "/admin/agents/extract"
        body = logged_in.get("/admin/agents").text
        assert "extract" in body
        assert "T2 · 结构化" in body

    def test_slug_is_readonly_when_editing(self, logged_in: TestClient):
        """标识发布后不能改——调用方的代码里写着它。"""
        self._create(logged_in)
        body = logged_in.get("/admin/agents/extract").text
        assert "readonly" in body

    def test_all_four_tiers_are_offered_with_their_costs(self, logged_in: TestClient):
        """四档都列出来，且每一档的代价都写在选项里。

        星槎的价值不是替用户选最强档，而是把权衡摆出来标注代价——竞品无一这么做。
        """
        import re

        body = logged_in.get("/admin/agents/new").text
        assert set(re.findall(r'<option value="(T\w+)"', body)) == {"T1", "T2", "T1P", "T3"}
        assert "对齐税" in body
        assert "提升为必填" in body

    def test_editing_creates_a_new_version(self, logged_in: TestClient):
        self._create(logged_in)
        self._create(logged_in, instructions="改过的指令")
        body = logged_in.get("/admin/agents/extract").text
        assert "v2" in body
        assert "回滚到这个版本" in body

    def test_bad_schema_returns_to_form_with_content(self, logged_in: TestClient):
        """跳到一个错误页会让人白填一遍。表单必须原地报错并保留内容。"""
        r = self._create(
            logged_in,
            slug="bad",
            output_schema='{"type":"object","properties":{"a":{"pattern":"(a+)+"}}}',
        )
        assert r.status_code == 200
        assert "pattern" in r.text
        assert 'value="bad"' in r.text

    def test_invalid_slug_is_rejected(self, logged_in: TestClient):
        r = self._create(logged_in, slug="BAD_SLUG")
        assert r.status_code == 200
        assert "标识" in r.text

    def test_agent_mutations_need_csrf(self, logged_in: TestClient):
        logged_in.get("/admin/agents/new")
        r = logged_in.post(
            "/admin/agents/save",
            data={"slug": "x", "name": "x", "instructions": "i", "model": "m"},
            follow_redirects=False,
        )
        assert r.status_code == 403


class TestSchemaLint:
    def test_flags_generic_and_short_names(self, logged_in: TestClient):
        """字段名本身是一条隐式指令通道——这是只有表单形态才方便提供的功能。"""
        logged_in.get("/admin/agents/new")
        r = logged_in.post(
            "/admin/agents/lint",
            data={
                "output_schema": (
                    '{"type":"object","properties":'
                    '{"data":{"type":"string"},"id":{"type":"string"}}}'
                ),
                "tier": "T2",
                "csrf_token": csrf_of(logged_in),
            },
        )
        assert r.status_code == 200
        assert "语义空泛" in r.text
        assert "太短" in r.text

    def test_stays_quiet_on_good_names(self, logged_in: TestClient):
        logged_in.get("/admin/agents/new")
        r = logged_in.post(
            "/admin/agents/lint",
            data={"output_schema": SCHEMA_TEXT, "tier": "T2", "csrf_token": csrf_of(logged_in)},
        )
        assert "语义空泛" not in r.text
        assert "太短" not in r.text

    def test_never_blocks_saving(self, logged_in: TestClient):
        """建议不是规则。一个把建议做成拦截的 lint 会很快被绕过或关掉。"""
        logged_in.get("/admin/agents/new")
        r = logged_in.post(
            "/admin/agents/save",
            data={
                "slug": "sloppy",
                "name": "x",
                "description": "",
                "instructions": "i",
                "model": "openai/gpt-5",
                "output_schema": '{"type":"object","properties":{"data":{"type":"string"}}}',
                "tier": "T2",
                "retries": "2",
                "csrf_token": csrf_of(logged_in),
            },
            follow_redirects=False,
        )
        assert r.status_code == 303, "命名不好只该给建议，不该拦住保存"

    def test_accepts_csrf_from_header_too(self, logged_in: TestClient):
        """页面里 HTMX 走 hx-headers，只认表单字段的话点了会没反应。"""
        logged_in.get("/admin/agents/new")
        r = logged_in.post(
            "/admin/agents/lint",
            data={"output_schema": SCHEMA_TEXT, "tier": "T2"},
            headers={"x-csrf-token": csrf_of(logged_in)},
        )
        assert r.status_code == 200


class TestAgentExport:
    def test_download_is_a_working_zip(self, logged_in: TestClient):
        """随时能带走——这是低锁定在后台里的兑现方式。"""
        import io
        import zipfile

        TestAgentForm()._create(logged_in)
        r = logged_in.get("/admin/agents/extract/export")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/zip"
        assert "extract-v1.zip" in r.headers["content-disposition"]

        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            names = set(zf.namelist())
            assert names == {
                "extract/agent.yaml",
                "extract/schema.json",
                "extract/run.py",
                "extract/README.md",
            }
            run_py = zf.read("extract/run.py").decode()
            assert "xingcha" not in run_py, "导出物不能依赖星槎"
            readme = zf.read("extract/README.md").decode()
            assert "丢失" in readme, "README 必须如实写清丢了什么"

    def test_export_needs_login(self, client: TestClient):
        r = client.get("/admin/agents/extract/export", follow_redirects=False)
        assert r.status_code == 303


class TestCsrfCookieLifetime:
    """CSRF cookie 必须和会话 cookie **同寿**。

    此前它没有 max_age，也就是浏览器会话级：关掉浏览器再打开，``xc_session``
    还在（7 天），``xc_csrf`` 已经没了。后果不是"要重新登录"——那反而好懂；
    而是那些**从 cookie 里取令牌**的表单静默 403，同一页里自己签发令牌的表单
    照常工作。症状是"只有某几个按钮不好使"，而两者的区别在模板里看不出来。

    侧栏的主题切换器正是前一类（它在每一页上，没有自己的渲染入口）。
    """

    @staticmethod
    def _max_age(raw: str) -> int | None:
        m = re.search(r"Max-Age=(\d+)", raw, re.I)
        return int(m.group(1)) if m else None

    def test_both_cookies_share_a_lifetime(self, client: TestClient):
        r = client.post(
            "/admin/login",
            data={"password": PASSWORD, "confirm": PASSWORD},
            follow_redirects=False,
        )
        # `headers.items()` 会把多个 Set-Cookie 合成一条（httpx 的行为），
        # 于是只有第一个 cookie 能被匹配到 —— 必须用 get_list。
        ages = {
            name: self._max_age(v)
            for v in r.headers.get_list("set-cookie")
            for name in ("xc_session", "xc_csrf")
            if v.startswith(f"{name}=")
        }
        assert ages.keys() == {"xc_session", "xc_csrf"}, f"少签了 cookie：{ages}"
        assert ages["xc_csrf"] is not None, "xc_csrf 没有 Max-Age —— 关掉浏览器就没了"
        assert ages["xc_csrf"] == ages["xc_session"], f"两个 cookie 寿命不一致：{ages}"

    def test_a_page_issued_token_also_carries_a_lifetime(self, logged_in: TestClient):
        """页面自己签发令牌那条路径（首次访问、cookie 不存在时）也要带 Max-Age。"""
        logged_in.cookies.delete("xc_csrf")
        r = logged_in.get("/admin/keys")
        raw = next((v for v in r.headers.get_list("set-cookie") if v.startswith("xc_csrf=")), "")
        assert raw, "没有重新签发 xc_csrf"
        assert self._max_age(raw), f"重新签发的 xc_csrf 没有 Max-Age：{raw}"


class TestThemeTokensStayInSync:
    """两个暗色 token 块必须声明**完全相同**的变量名。

    重复是 CSS 逼出来的：一份在 ``@media (prefers-color-scheme: dark)`` 里（跟随
    系统），一份在 ``:root[data-theme="dark"]`` 里（用户显式选暗色，以及登录页强制
    暗色）。没有办法在纯 CSS 里让一个声明块同时挂在这两个上下文上。

    既然重复不可避免，就必须让**漏一个**这件事变红。漏了的后果实测过：登录页强制
    ``data-theme="dark"``，而 ``--star-opacity`` 只在 @media 那份里定义，于是回落到
    ``:root`` 的 0 —— **星星全没了，页面一片黑**，而系统本来就是暗色的人完全看不出
    问题（对他们两个块都命中）。
    """

    @staticmethod
    def _names(block: str) -> set[str]:
        return set(re.findall(r"(--[a-z0-9-]+)\s*:", block))

    @pytest.fixture(scope="class")
    def css(self) -> str:
        return (
            Path(__file__).resolve().parent.parent / "src/xingcha/web/static/style.css"
        ).read_text(encoding="utf-8")

    def test_both_dark_blocks_declare_the_same_tokens(self, css: str):
        media = css[css.index('  :root:not([data-theme="light"]) {') :]
        media = media[: media.index("\n  }")]
        explicit = css[css.index(':root[data-theme="dark"] {') :]
        explicit = explicit[: explicit.index("\n}")]

        only_media = self._names(media) - self._names(explicit)
        only_explicit = self._names(explicit) - self._names(media)
        assert not only_media, f"只在 @media 里定义了：{sorted(only_media)}"
        assert not only_explicit, f"只在 [data-theme=dark] 里定义了：{sorted(only_explicit)}"

    def test_the_galaxy_needs_its_tokens_in_both(self, css: str):
        """点名银河那几个：它们是最容易漏的——只有登录页用，而登录页恒为
        ``data-theme="dark"``，也就是**只走显式那一块**。"""
        explicit = css[css.index(':root[data-theme="dark"] {') :]
        explicit = explicit[: explicit.index("\n}")]
        for token in ("--star-opacity", "--star-rgb", "--milk-core", "--milk-spine"):
            assert token in explicit, f"[data-theme=dark] 里缺 {token}，登录页会一片黑"

    def test_stars_are_visible_in_the_dark_palette(self, css: str):
        """``--star-opacity`` 在暗色里必须是 1。

        亮色里它是 0（白底上的星点像脏点），所以这一格写错的表现不是"难看"，
        而是"星星完全消失"，且不报任何错。
        """
        explicit = css[css.index(':root[data-theme="dark"] {') :]
        explicit = explicit[: explicit.index("\n}")]
        assert re.search(r"--star-opacity:\s*1\s*;", explicit)
