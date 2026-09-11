"""部署产物之间的一致性。

这些数字与属性散在 Caddyfile、两份 compose、一个 sh 和三个 ps1 里，**没有任何语言
层面的机制能让它们保持相等**。漂移的症状有个共同点：服务本身好着，只是后台印出一个
打不开的地址、或者 cookie 悄悄少了一层保护——不会有人看见报错。

所以这里逐条钉死。改端口就改一处然后让这个文件红，而不是指望下次部署时发现。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from xingcha import contract as C

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"

CADDYFILE = DEPLOY / "edge" / "Caddyfile"
COMMON_PS1 = DEPLOY / "_common.ps1"
EDGE_PS1 = DEPLOY / "edge" / "edge.ps1"
XC_SH = DEPLOY / "linux" / "xc"
COMPOSE = DEPLOY / "linux" / "docker-compose.yml"
COMPOSE_GATEWAY = DEPLOY / "linux" / "docker-compose.gateway.yml"
DOCKERFILE = ROOT / "Dockerfile"

#: 网关对外的唯一 HTTPS 端口。真值在 Caddyfile 里，其余都是抄的。
GATEWAY_PORT = 8443
#: 星槎自己的明文端口。挂网关时只绑回环。
APP_PORT = 8720


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8-sig")


def test_all_deploy_files_exist():
    # 少一个的症状是"某个平台的启动路径整个没了"，而另一个平台照常绿。
    for p in (CADDYFILE, COMMON_PS1, EDGE_PS1, XC_SH, COMPOSE, COMPOSE_GATEWAY, DOCKERFILE):
        assert p.is_file(), f"缺少部署产物：{p.relative_to(ROOT)}"


# =============================================================================
# 端口：同一个数字抄在四五个地方
# =============================================================================


def test_caddyfile_is_the_source_of_truth():
    text = _text(CADDYFILE)
    assert f":{GATEWAY_PORT} {{" in text, f"Caddyfile 的站点地址不再是 :{GATEWAY_PORT}"
    assert f"reverse_proxy 127.0.0.1:{APP_PORT}" in text, (
        f"Caddyfile 的反代目标不再是 127.0.0.1:{APP_PORT}"
    )


def test_powershell_common_matches_caddyfile():
    """``_common.ps1`` 里的两个端口是从 Caddyfile 抄来的，必须相等。

    这条正是 ``_common.ps1`` 第 31 行注释所说的"有一条测试盯着两处相等"。不相等的
    症状：后台印出一个打不开的地址，而服务本身好着。
    """
    text = _text(COMMON_PS1)
    assert re.search(rf"\$script:GatewayPort\s*=\s*{GATEWAY_PORT}\b", text)
    assert re.search(rf"\$script:DefaultPort\s*=\s*{APP_PORT}\b", text)


def test_edge_ps1_matches_caddyfile():
    text = _text(EDGE_PS1)
    assert re.search(rf"\$Port\s*=\s*{GATEWAY_PORT}\b", text)
    assert re.search(rf'\$Upstream\s*=\s*"127\.0\.0\.1:{APP_PORT}"', text)


def test_linux_xc_matches_caddyfile():
    text = _text(XC_SH)
    assert re.search(rf"^\s*p={GATEWAY_PORT}\s*$", text, re.M), (
        f"deploy/linux/xc 的 public_port() 不再返回 {GATEWAY_PORT}"
    )
    assert f"port={APP_PORT}" in text or f"p={APP_PORT}" in text


def test_container_listens_on_the_contract_port():
    # 容器内的监听端口由编排写死，而 Caddy 反代的就是它。
    compose = yaml.safe_load(_text(COMPOSE))
    env = compose["services"]["xingcha"]["environment"]
    assert str(env["XINGCHA_PORT"]) == str(APP_PORT)
    assert env["XINGCHA_HOST"] == "0.0.0.0"  # 容器内绑全 0，隔离靠宿主那一侧的映射


# =============================================================================
# 安全属性：默认只绑回环
# =============================================================================


def test_published_port_defaults_to_loopback():
    """宿主端口的默认值必须是 127.0.0.1。

    映射出去的端口走 Docker 的 DOCKER-USER 链、**绕过 ufw**，而它那一头是明文 HTTP。
    所以"开给局域网"必须是一次显式选择，默认值不能是 0.0.0.0。

    这也是 CI 里那条 ``grep host_ip: 127.0.0.1`` 真正依赖的东西——CI 的 .env 里只有
    ``XINGCHA_GATEWAY=edge``，``XINGCHA_BIND_ADDR`` 走的就是这个默认值。
    """
    compose = yaml.safe_load(_text(COMPOSE))
    ports = compose["services"]["xingcha"]["ports"]
    assert len(ports) == 1
    assert ports[0].startswith("${XINGCHA_BIND_ADDR:-127.0.0.1}:"), (
        f"宿主端口映射的默认绑定地址不再是 127.0.0.1：{ports[0]!r}"
    )


def test_gateway_overlay_ships_both_halves():
    """叠加层的两项**要么全有要么全无**。

    只留一项的后果是安静的：PUBLIC_URL 是 https 而 TRUSTED_PROXIES 没配 → 应用看到
    的 scheme 是 http（网关到这里那一跳走回环），会话 cookie 不带 Secure，而功能完全
    正常，没人会注意到。反过来同样。

    这正是它是一个**文件**而不是两个 ``${}`` 变量的理由——compose 的插值表达不了
    "成套出现"。删掉其中一项，这条会红。
    """
    overlay = yaml.safe_load(_text(COMPOSE_GATEWAY))
    env = overlay["services"]["xingcha"]["environment"]
    assert env["XINGCHA_PUBLIC_URL"].startswith("https://"), "挂网关时对外地址必须是 https"
    assert env["XINGCHA_TRUSTED_PROXIES"] == "*", (
        "挂网关时必须信任转发头，否则会话 cookie 不带 Secure"
    )
    assert f"{GATEWAY_PORT}" in env["XINGCHA_PUBLIC_URL"], "对外地址的端口必须是网关的"


def test_overlay_only_touches_the_same_service():
    # 叠加层里多出一个服务名 = 那一段配置永远不生效，而 compose 不会报错。
    base = yaml.safe_load(_text(COMPOSE))
    overlay = yaml.safe_load(_text(COMPOSE_GATEWAY))
    assert set(overlay["services"]) <= set(base["services"])


def test_compose_project_name_is_pinned():
    """项目名写死。

    不写的话 compose 用 compose 文件所在目录名（``deploy``），容器会叫
    ``deploy-xingcha-1``，而所有文档、脚本与运维记忆里都是 ``xingcha-xingcha-1``。
    """
    assert yaml.safe_load(_text(COMPOSE))["name"] == "xingcha"


# =============================================================================
# 契约 ↔ 部署
# =============================================================================


def test_dockerfile_uid_matches_contract():
    """容器里的 UID 必须等于 ``contract.CONTAINER_UID``。

    ``deploy/linux/xc`` 是从契约文件里读这个值去 chown 宿主的 data/ 的；Dockerfile
    这一侧是写死的。两边不等的症状是容器起来就进重启循环——写不进 data 目录。
    """
    assert re.search(rf"useradd --uid {C.CONTAINER_UID}\b", _text(DOCKERFILE)), (
        f"Dockerfile 的 UID 与 contract.CONTAINER_UID（{C.CONTAINER_UID}）不一致"
    )


def test_xc_actually_extracts_the_uid_from_the_contract():
    """``deploy/linux/xc`` 的 ``from_contract`` 必须真的读得出值来。

    它不抄字面量，而是 ``sed`` 一个源码文件——**那条路径没有任何东西在维护**。
    上一次 ``contract.py`` → ``contract/__init__.py`` 的重构就把它读空了，于是
    ``xc`` 在第 40 行 ``exit 1``，整条 Linux docker 部署路径当场死掉，而仓库里
    一切看起来都好。

    所以这里照着脚本里的写法重跑一遍提取，而不是 grep 一个字符串——grep 得到的
    绿灯正是那次漂移时挂着的那一盏。
    """
    text = _text(XC_SH)
    m = re.search(r"from_contract\(\)\s*\{[^}]*?\s(\S+\.py)\s*\|", text)
    assert m, "deploy/linux/xc 里找不到 from_contract() 的源码路径"

    target = ROOT / m.group(1)
    assert target.is_file(), (
        f"deploy/linux/xc 读的是 {m.group(1)}，而这个文件不存在——"
        f"脚本会在启动时 exit 1。契约模块被挪动过？"
    )

    # 照搬 sed 的语义：行首常量名、取 = 右边的第一串数字。
    found = re.search(r"^CONTAINER_UID[^=]*=\s*(\d+)", target.read_text(encoding="utf-8"), re.M)
    assert found and int(found.group(1)) == C.CONTAINER_UID, (
        f"xc 从 {m.group(1)} 里读不出 CONTAINER_UID（期望 {C.CONTAINER_UID}）"
    )


# =============================================================================
# PowerShell 的编码陷阱
# =============================================================================


@pytest.mark.parametrize("path", [DEPLOY / "windows" / "xc.bat", DEPLOY / "edge" / "edge.bat"])
def test_bat_files_are_pure_ascii(path: Path):
    """``.bat`` 里**一个非 ASCII 字节都不许有**。

    cmd.exe 在 ``chcp 65001`` 下按字节偏移回溯文件位置，会从一个汉字中间接着读，
    **后半行被当成一条新命令执行**——``rem … reset-password 忘了密码`` 那行注释真的
    被跑过一次，而且时有时无。所以 .bat 只做启动器，中文全在同名 .ps1 里。
    """
    raw = path.read_bytes()
    bad = [(i, b) for i, b in enumerate(raw) if b > 0x7F]
    assert not bad, (
        f"{path.relative_to(ROOT)} 第 {bad[0][0]} 字节起有非 ASCII 内容"
        f"（共 {len(bad)} 个）。中文提示请放进同名的 .ps1。"
    )


@pytest.mark.parametrize("path", [COMMON_PS1, EDGE_PS1, DEPLOY / "windows" / "xc.ps1"])
def test_powershell_files_have_utf8_bom(path: Path):
    """``.ps1`` 必须存成**带 BOM 的 UTF-8**。

    PowerShell 5.1（Windows 自带的那个）读无 BOM 的文件按 ANSI 码页解码，于是脚本里
    所有中文提示变成乱码；更糟的是字符串比较也会跟着错。这是一个存盘时很容易丢、
    而症状看起来完全不像编码问题的属性。
    """
    assert path.read_bytes()[:3] == b"\xef\xbb\xbf", f"{path.relative_to(ROOT)} 缺少 UTF-8 BOM"
