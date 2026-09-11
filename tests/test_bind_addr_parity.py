"""Windows 与 Linux 必须对同一份 ``.env`` 推出同一个绑定地址。

两条部署路各自实现了一遍"绑哪儿"：``deploy/linux/xc`` 的 ``derive_bind_addr``（sh）
与 ``deploy/_common.ps1`` 的 ``Resolve-Deployment``（PowerShell）。同一份规则写两遍，
没有任何语言机制让它们保持一致。

实际漂移过，而且是往**更暴露**的方向：``XINGCHA_WEB_HOST`` 留空时，Windows 那边先把
它替换成探测到的内网 IP，再拿替换后的值去判断该绑哪儿，于是落进 ``else`` 绑了
``0.0.0.0``——同一份 .env，Linux 上只绑回环，Windows 上把明文 HTTP 开给了整个局域网。
而"开给局域网"按设计必须是一次**显式**选择。

Linux 那条规则短到可以在这里重写一遍（就下面 ``_linux_bind``，四行），所以这里拿它
当基准，去跑真正的 PowerShell 函数比对。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMMON_PS1 = ROOT / "deploy" / "_common.ps1"

#: 覆盖三类分支：留空、显式回环、显式对外；每类再乘上挂不挂网关。
CASES = [
    ("", ""),
    ("", "localhost"),
    ("", "127.0.0.1"),
    ("", "0.0.0.0"),
    ("", "192.168.1.5"),
    ("", "xc.lan"),
    ("edge", ""),
    ("edge", "localhost"),
    ("edge", "0.0.0.0"),
    ("edge", "192.168.1.5"),
]


def _linux_bind(gateway: str, web_host: str) -> str:
    """``deploy/linux/xc`` 的 ``derive_bind_addr``，逐条照抄。

    照抄而不是去 source 那个 sh：CI 是 Linux、开发机是 Windows，跑 sh 会让这条测试
    在一半机器上变成 skip，而 skip 掉的守卫和没有守卫是一回事。
    """
    if gateway:
        return "127.0.0.1"
    if web_host in ("", "localhost", "127.0.0.1"):
        return "127.0.0.1"
    return "0.0.0.0"


def _powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


@pytest.fixture(scope="module")
def windows_binds(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    """把全部用例喂给真正的 ``Resolve-Deployment``，一次调用取回全部结果。"""
    shell = _powershell()
    if shell is None:
        # CI 上必须真跑。装不了 PowerShell 的开发机才允许跳过——否则这条守卫会在
        # 最该生效的地方（CI）静悄悄地不工作，还挂着一盏绿灯。
        if os.environ.get("CI"):
            pytest.fail("CI 上找不到 pwsh/powershell，这条守卫会静默失效")
        pytest.skip("本机没有 PowerShell")

    workdir = tmp_path_factory.mktemp("binds")
    script = workdir / "probe.ps1"
    script.write_text(
        textwrap.dedent(f"""
        . '{COMMON_PS1.as_posix()}'
        $out = @{{}}
        $cases = @({",".join(f"@{{g='{g}'; h='{h}'}}" for g, h in CASES)})
        foreach ($c in $cases) {{
            $f = Join-Path '{workdir.as_posix()}' 'c.env'
            Set-Content $f "XINGCHA_GATEWAY=$($c.g)`nXINGCHA_WEB_HOST=$($c.h)" -Encoding utf8
            $out["$($c.g)|$($c.h)"] = (Resolve-Deployment -EnvPath $f).BindAddr
        }}
        $out | ConvertTo-Json -Compress
        """).strip(),
        encoding="utf-8",
    )

    proc = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"PowerShell 跑挂了：\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("gateway, web_host", CASES, ids=lambda v: repr(v))
def test_bind_addr_matches_linux(gateway: str, web_host: str, windows_binds: dict[str, str]):
    expected = _linux_bind(gateway, web_host)
    actual = windows_binds[f"{gateway}|{web_host}"]
    assert actual == expected, (
        f"GATEWAY={gateway!r} WEB_HOST={web_host!r} 时两条路不一致："
        f"Linux 绑 {expected}，Windows 绑 {actual}。"
    )


def test_empty_web_host_stays_on_loopback(windows_binds: dict[str, str]):
    """留空不是"开给局域网"的意思。

    单列一条：上面那批是"两边一致"，万一哪天两边**一起**改错，这条仍然会红。
    绑到局域网的那个端口那一头是明文 HTTP，密码与 sk-xc- 都在上面裸传。
    """
    assert windows_binds["|"] == "127.0.0.1"


def test_explicit_wildcard_still_opens_up(windows_binds: dict[str, str]):
    """显式写 0.0.0.0 仍然要开出去——别把 bug 修成"永远绑不了局域网"。"""
    assert windows_binds["|0.0.0.0"] == "0.0.0.0"
