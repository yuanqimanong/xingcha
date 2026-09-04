"""管理后台的安全性。

后台暴露在公网上，里面有一个能改写上游 base_url 的表单。**A1 与 A2 组合起来就是
一次点击盗走付费 key**：让管理员的浏览器 POST 一次把 base_url 指向攻击者，
下一次调用就把 key 送上门。所以这里的每一条都不是"锦上添花"。

注意 ``base_url="https://testserver"``：会话 cookie 是 ``Secure`` 的，用 http 测
客户端根本不会回传它，测出来的"通过"是假的。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient

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


class TestSettingsMutation:
    def test_requires_password_confirmation(self, logged_in: TestClient):
        """改上游配置是全后台后果最严重的操作，CSRF 三层之外再加一道。"""
        logged_in.get("/admin/settings")
        r = logged_in.post(
            "/admin/settings/upstream",
            data={
                "password": "wrong-password",
                "api_key": "sk-or-v1-attacker",
                "csrf_token": csrf_of(logged_in),
            },
            follow_redirects=False,
        )
        assert r.status_code == 403
        assert "密码不正确" in r.text

    def test_dangerous_base_url_is_rejected_even_with_password(self, logged_in: TestClient):
        logged_in.get("/admin/settings")
        r = logged_in.post(
            "/admin/settings/upstream",
            data={
                "password": PASSWORD,
                "base_url": "http://169.254.169.254/",
                "csrf_token": csrf_of(logged_in),
            },
            follow_redirects=False,
        )
        assert r.status_code == 403
        assert "元数据" in r.text

    def test_valid_update_succeeds(self, logged_in: TestClient):
        logged_in.get("/admin/settings")
        r = logged_in.post(
            "/admin/settings/upstream",
            data={
                "password": PASSWORD,
                "api_key": "sk-or-v1-legit",
                "csrf_token": csrf_of(logged_in),
            },
            follow_redirects=False,
        )
        assert r.status_code == 303

    def test_saved_key_is_masked_in_ui(self, logged_in: TestClient):
        """页面上不能出现完整的上游 key —— 截图、录屏、肩窥都是真实路径。"""
        self.test_valid_update_succeeds(logged_in)
        body = logged_in.get("/admin/settings").text
        assert "sk-or-v1-legit" not in body
        assert "***" in body


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

    def test_only_implemented_tiers_are_offered(self, logged_in: TestClient):
        """T1 需要先有"可选字段会被提升为必填"的提示才能安全开放。"""
        import re

        body = logged_in.get("/admin/agents/new").text
        assert set(re.findall(r'<option value="(T\w+)"', body)) == {"T2", "T3"}

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
