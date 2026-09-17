"""后台被别的门户嵌进 iframe 时的准入。

这条路径上有两道门，**而且两道都会以"看起来像坏了"的方式失败**：

* ``frame-ancestors 'none'`` + ``X-Frame-Options: DENY`` —— iframe 渲染出一块空白，
  除了浏览器控制台没有任何提示；
* 同源校验 —— 门户若用同源反代把后台挂在自己的路径下，浏览器发来的 ``Origin`` 是门户
  的源，与我们看到的 ``Host`` 永远不符，登录一提交就被拒，而拒绝的措辞与"真有人跨站
  打你"完全一样。

两道门都只在配了 ``XINGCHA_ADMIN_EMBED_ORIGINS`` 时对**指名的来源**放开。这里同时钉住
放开的样子与**没配时一个字都不变**——后者才是绝大多数部署的处境。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from starlette.requests import Request
from starlette.responses import Response

from xingcha.config import ENV_PREFIX, Settings
from xingcha.web.admin import security

PORTAL = "http://43.163.9.107:30800"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """模块级配置用完必须还原，而 ``Settings`` 必须在一个干净的世界里构造。

    ``Settings`` 有两条不经过构造参数的输入：``model_config`` 里的 ``env_file=".env"``
    ——相对路径，按**当前工作目录**解析——以及 ``XINGCHA_`` 前缀的环境变量。两条都会把
    「没配时是什么样」的断言悄悄换成「这台机器上配了什么」。

    本仓库根目录的 ``.env`` 恰好配了 ``XINGCHA_ADMIN_EMBED_ORIGINS``，于是
    :func:`test_settings_empty_means_none` 在开发机上必红、在干净的 CI 上却是绿的。
    这种红最费人：它和你正在改的东西无关，但每次都要重新判断一遍「这条是不是我弄坏的」。

    chdir 到空目录切掉前者，delenv 切掉后者。放 autouse 而不是写进各个用例：以后往这个
    文件里加用例的人不该需要先知道这件事。
    """
    monkeypatch.chdir(tmp_path)
    for name in [k for k in os.environ if k.startswith(ENV_PREFIX)]:
        monkeypatch.delenv(name, raising=False)
    yield
    security.configure(())


def _request(**headers: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/admin/login",
            "query_string": b"",
            "scheme": "http",
            "server": ("10.20.1.13", 8720),
            "headers": [(k.replace("_", "-").encode(), v.encode()) for k, v in headers.items()],
        }
    )


# =============================================================================
# 没配时：与从前逐字相同
# =============================================================================


def test_default_denies_all_framing():
    resp = security.security_headers(Response())
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in resp.headers["Content-Security-Policy"]


def test_default_rejects_portal_origin():
    security.configure(())
    with pytest.raises(security.Denied):
        security.check_origin(_request(origin=PORTAL, host="10.20.1.13:8720"))


# =============================================================================
# 配了之后：只放行指名的来源
# =============================================================================


def test_configured_origin_replaces_frame_ancestors():
    security.configure([PORTAL])
    resp = security.security_headers(Response())
    csp = resp.headers["Content-Security-Policy"]
    assert f"frame-ancestors {PORTAL}" in csp
    # 其余指令一条都不能少——收的是 frame-ancestors，不是整份策略
    assert "default-src 'self'" in csp
    assert "form-action 'self'" in csp
    assert "base-uri 'none'" in csp


def test_configured_drops_x_frame_options():
    """X-Frame-Options 只有 DENY / SAMEORIGIN 两档，表达不了"只允许某个源"。

    留着 DENY 会把 CSP 刚放行的那个源又挡回去，而且是浏览器优先采信的那一个——
    症状是"配了名单还是白屏"。
    """
    security.configure([PORTAL])
    assert "X-Frame-Options" not in security.security_headers(Response()).headers


def test_configured_origin_passes_check():
    security.configure([PORTAL])
    security.check_origin(_request(origin=PORTAL, host="10.20.1.13:8720"))


def test_configured_origin_passes_even_when_marked_cross_site():
    """直接跨源嵌入时浏览器会报 ``Sec-Fetch-Site: cross-site``，那条分支也得放行。

    同源反代那种嵌法走不到这里（浏览器眼里一切都是同源），但两种嵌法都该能用。
    """
    security.configure([PORTAL])
    security.check_origin(
        _request(origin=PORTAL, host="10.20.1.13:8720", sec_fetch_site="cross-site")
    )


def test_other_origin_still_rejected():
    """放行的是名单，不是"凡是跨站都放行"。"""
    security.configure([PORTAL])
    with pytest.raises(security.Denied):
        security.check_origin(
            _request(origin="http://somewhere.example.com", host="10.20.1.13:8720")
        )


def test_trailing_slash_normalised():
    """浏览器发的 Origin 永远不带末尾斜杠，配置里多写一个就是永远匹配不上。"""
    security.configure(Settings(admin_embed_origins=f"{PORTAL}/").embed_origins)
    security.check_origin(_request(origin=PORTAL, host="10.20.1.13:8720"))


# =============================================================================
# 配置解析
# =============================================================================


def test_settings_parse_multiple():
    s = Settings(admin_embed_origins=f" {PORTAL} , http://portal.internal:8080 ")
    assert s.embed_origins == (PORTAL, "http://portal.internal:8080")


def test_settings_empty_means_none():
    assert Settings().embed_origins == ()
    assert Settings(admin_embed_origins="").embed_origins == ()
