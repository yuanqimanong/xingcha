<#
.SYNOPSIS
    星槎 · Windows 本地直跑。真正干活的是这个脚本，xc.bat 只是启动器。

.DESCRIPTION
    为什么逻辑在 .ps1 而不是 .bat：cmd.exe 在 chcp 65001 下按字节偏移回溯文件位置，
    而偏移记账在多字节字符上是错的，于是会从一个汉字中间接着读——后半行被当成一条
    新命令**执行**。踩过一次真的：`rem ... reset-password  忘了密码` 那行注释被跑了。
    而且它时有时无（文件不在页缓存里时更容易撞），属于最难查的一类。

    **这个文件必须存成带 BOM 的 UTF-8**，和 .bat 正好相反：Windows PowerShell 5.1
    读无 BOM 的 .ps1 按系统 ANSI 解码，中文直接乱掉。

    语法只用 5.1 认的：不用 `?.`、`??`、三元。

    为什么 Windows 上不走 docker、.env 怎么配、防火墙那一步——见同目录的 XC.md。
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }

function Say  { param($m) Write-Host "  $m" }
function Ok   { param($m) Write-Host "OK $m" -ForegroundColor Green }
function Warn { param($m) Write-Host "  ! $m" -ForegroundColor Yellow }
function Die  { param($m) Write-Host ""; Write-Host "[x] $m" -ForegroundColor Red; exit 1 }

# 仓库根 = 脚本所在目录的**上两级**。少上一级就会在 deploy\ 下面又建一个 data\，
# 症状是「我明明起来了，后台却是空的」，而两个 data 都真实存在，看不出哪个是对的。
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Resolve-Path (Join-Path $Here "..\..")
Set-Location $Root

# 地址那几项**一处推导**，与 edge.ps1 和 deploy/linux/xc 同一套规则。
. (Join-Path $Here "..\_common.ps1")

# ---------------------------------------------------------------------------
# uv
# ---------------------------------------------------------------------------
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host ""
    Write-Host "[x] 找不到 uv。装一次就够了，两种随便挑：" -ForegroundColor Red
    Write-Host ""
    Say "    winget install --id astral-sh.uv"
    Say '    powershell -c "irm https://astral.sh/uv/install.ps1 | iex"'
    Write-Host ""
    Say "装完**重开一个窗口**再双击 —— PATH 是进程启动时读的，"
    Say "当前这个窗口拿不到刚装上的东西。"
    exit 1
}

# ---------------------------------------------------------------------------
# .env —— 和 Linux 用**同一份模板**，不另起一份（理由见 XC.md）
# ---------------------------------------------------------------------------
if (-not (Test-Path ".env")) {
    Say ".env 不存在，从模板生成一份"
    Copy-Item "deploy\.env.example" ".env" -Force
    Ok "已生成 .env"
    Write-Host ""
    Say "一项都不改也能起来，但**明文 HTTP 且只绑回环**：只有这台机器能打开。"
    Say "要 HTTPS：双击 deploy\edge\edge.bat 起网关，然后翻到 .env 最后一节"
    Say "把这两行放开（为什么见 deploy\windows\XC.md）："
    Write-Host ""
    Say "    XINGCHA_TRUSTED_PROXIES=127.0.0.1"
    Say "    XINGCHA_PUBLIC_URL=https://本机内网IP:8443"
    Write-Host ""
}

# ---------------------------------------------------------------------------
# 依赖
#
# --frozen：严格照 uv.lock 装，不许就地改锁文件。部署机上「顺手升了个依赖」是最难
#   查的一类差异——代码一个字没动，行为变了。
# --no-dev：dev 组里有 playwright，几百 MB，跑服务用不上。**注意它会把 dev 依赖从
#   .venv 里删掉**，之后要跑测试先 `uv sync --frozen` 补回来。
# ---------------------------------------------------------------------------
Say "同步依赖（第一次要下 Python 和依赖包，几分钟；之后是秒级）"
& uv sync --frozen --no-dev
if ($LASTEXITCODE -ne 0) { Die "uv sync 失败，原因在上面。" }

# ---------------------------------------------------------------------------
# 地址
#
# 用户在 .env 里只填三项（GATEWAY / WEB_HOST / WEB_PORT），应用真正要的四项
# ——HOST / PORT / PUBLIC_URL / TRUSTED_PROXIES——由 _common.ps1 推出来，在这里
# 塞进子进程的环境。
#
# 为什么不让用户直接填那四项（以前就是）：
#   · XINGCHA_WEB_HOST 与 XINGCHA_HOST 名字像、作用完全不同（前者应用根本不读），
#     想"让别人能访问"而只改了前者，是个静默空操作；
#   · "挂了网关"这件事要在四个地方分别表达一遍，只改一处的后果同样安静——
#     比如 PUBLIC_URL 写了 https 而 TRUSTED_PROXIES 没配，于是会话 cookie 不带
#     Secure，功能完全正常，没人会注意到。
#
# docker 那条路早就是推出来的（deploy/linux/xc 的 derive_bind_addr / public_port），
# 这里补上，两条路才是同一套行为。
# ---------------------------------------------------------------------------
$d = Resolve-Deployment (Join-Path $Root ".env")

$env:XINGCHA_HOST = $d.BindAddr
$env:XINGCHA_PORT = $d.Port
$env:XINGCHA_PUBLIC_URL = $d.PublicUrl
$env:XINGCHA_TRUSTED_PROXIES = $d.TrustedProxies

# 把两个地址的**关系**说清楚，而不是并排印两条。
#
# 只印两行地址的话，看起来像"起了两个服务、http 和 https 都有"——实际只有一个
# 进程，它只监听 $($d.BindAddr):$($d.Port)，HTTPS 是前面那个 Caddy 加的。
# uvicorn 自己还会再印一行，压不掉也不该压（它说的是进程实况），所以先把话说前面。
Write-Host ""
Write-Host "  入口  " -NoNewline
Write-Host $d.PublicUrl -ForegroundColor Green
if ($d.AutoDetected) {
    Say "        （WEB_HOST 没写死，这个 IP 是探出来的）"
}
if ($d.Gateway) {
    Say "        浏览器与调用方用这一个。HTTPS 由 deploy\edge\edge.bat 那个 Caddy 提供，"
    Say "        **它得在跑着**，否则这个地址打不开。"
    Write-Host ""
    Say "  内部   http://$($d.BindAddr):$($d.Port)"
    Say "        星槎自己只听这一个，**只绑回环**：别的机器连不上，也不该拿它当入口。"
    Say "        Caddy 就是从这里取内容的。下面 uvicorn 还会再印一次它。"
    Write-Host ""
    Say "  浏览器拦证书 = 这台设备还没装根证书：edge.bat trust"
} else {
    Say "        **明文 HTTP**（.env 里没配 XINGCHA_GATEWAY）。"
    if ($d.BindAddr -eq "0.0.0.0") {
        Write-Host ""
        Warn "绑在 0.0.0.0 上，端口是真的开在局域网里的：一条**明文**入口，谁都能直连。"
        Say  "  要 HTTPS：.env 里设 XINGCHA_GATEWAY=edge，再双击 deploy\edge\edge.bat。"
        Say  "  另外第一次跑 Windows 防火墙会弹窗问要不要放行 python.exe："
        Say  "  **必须点允许，而且要勾「专用网络」**，否则别的机器一直连不上，"
        Say  "  而这台机器自己完全正常 —— 那个现象指不到防火墙。"
    } else {
        Say "        只绑回环，只有这台机器能打开。"
    }
}

Write-Host ""
Say "启动。**关掉这个窗口就是停止**，data 不动。"
Write-Host ""

# 监听地址、端口、数据目录一律由 .env 决定，这里不传 --host / --port。
# 传了就是第二个来源，而两个来源必然有一天不一致。
& uv run --frozen --no-dev xingcha serve
if ($LASTEXITCODE -ne 0) { Die "服务退出，原因在上面。" }
exit 0
