<#
.SYNOPSIS
    这台机器的网关（Caddy 单文件）。真正干活的是这个脚本，edge.bat 只是启动器。

.DESCRIPTION
    为什么逻辑在 .ps1 而不是 .bat：

    cmd.exe 在 chcp 65001 下解析 .bat 时**按字节偏移回溯文件位置**，而偏移量的记账
    在多字节字符上是错的。于是它会从一个汉字中间接着读——前半截字节留在上一行，
    后半截落单（控制台显示成两个方块），而**后半行的内容被当成一条新命令执行**。
    这不是显示问题：`rem ... reset-password  忘了密码` 那行注释真的被跑过一次。

    更难办的是它**时有时无**：文件不在系统页缓存里（刚改过、刚开机）时更容易撞上，
    之后又"好了"。所以是那种偶发一次、下次复现不了的故障。

    PowerShell 没有这个 bug。所以 .bat 只剩纯 ASCII 的几行启动器，中文与逻辑全在
    这里。

    **这个文件必须存成带 BOM 的 UTF-8。** 和 .bat 正好相反：Windows PowerShell 5.1
    读无 BOM 的 .ps1 会按系统 ANSI 解码，中文直接乱掉。tests/test_deploy_artifacts.py
    里有一条断言盯着。

    语法只用 Windows PowerShell 5.1 认的：**不用 `?.`、`??`、三元** —— 那些是
    PowerShell 7 的，5.1 上是解析错误，而 5.1 是 Windows 自带的那个版本。

    完整说明见同目录的 CADDY.md。

.PARAMETER Verb
    get / run / start / stop / reload / trust / ca / status，默认 run。
#>
[CmdletBinding()]
param([string]$Verb = "run")

$ErrorActionPreference = "Stop"

# 控制台按 UTF-8 输出，否则中文在 GBK 控制台上是乱码。只改本进程，不动全局 chcp。
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }

# 认这一份 Caddyfile，不是"你在哪儿敲命令"。
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Here

# 证书名 / 端口 / 绑定那几项**一处推导**，与 xc.ps1 和 deploy/linux/xc 同一套规则。
. (Join-Path $Here "..\_common.ps1")

$RootCrt = Join-Path $env:AppData "Caddy\pki\authorities\local\root.crt"
$Port = 8443
$Upstream = "127.0.0.1:8720"
$LocalCaddy = Join-Path $Here "caddy.exe"

function Say  { param($m) Write-Host "  $m" }
function Ok   { param($m) Write-Host "OK $m" -ForegroundColor Green }
function Warn { param($m) Write-Host "  ! $m" -ForegroundColor Yellow }
function Die  { param($m) Write-Host ""; Write-Host "[x] $m" -ForegroundColor Red; exit 1 }

# ---------------------------------------------------------------------------
# 二进制。**认本目录的，优先于 PATH 里的。**
#
# 两个版本同时在的时候，「我明明升级了」却跑着旧的那个，是最费时的一类困惑。
# ---------------------------------------------------------------------------
function Resolve-Caddy {
    if (Test-Path $LocalCaddy) { return $LocalCaddy }
    $found = Get-Command caddy.exe -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    return $null
}

function Get-Caddy {
    if (Test-Path $LocalCaddy) {
        Ok "已经有了：$LocalCaddy（要换版本先删掉它）"
        return
    }
    Say "下载 caddy.exe（约 45 MB，不进版本库）"
    try {
        Invoke-WebRequest -Uri "https://caddyserver.com/api/download?os=windows&arch=amd64" -OutFile $LocalCaddy -UseBasicParsing
    } catch {
        Die "下载失败：$($_.Exception.Message)`n    到 https://caddyserver.com/download 手动拿一个，存成 $LocalCaddy"
    }
    Ok $LocalCaddy
    Say "接着：双击 edge.bat 就起来了。"
}

# ---------------------------------------------------------------------------
# 信任库
# ---------------------------------------------------------------------------
function Test-RootTrusted {
    foreach ($store in @("Cert:\CurrentUser\Root", "Cert:\LocalMachine\Root")) {
        try {
            $hit = Get-ChildItem $store -ErrorAction Stop | Where-Object { $_.Subject -like "*Caddy*" }
            if ($hit) { return $true }
        } catch { }
    }
    return $false
}

# 只提醒，不阻塞。Caddyfile 里有 skip_install_trust（理由见 CADDY.md），所以起服务
# 不再自动装证书、也不会被 UAC 弹窗卡死。代价是「浏览器会拦证书」没人主动告诉你，
# 而它的症状（打不开）和「网关压根没起来」长得一模一样。这条提示补的是这个。
function Show-TrustHint {
    if (Test-RootTrusted) { return }
    Warn "根证书还没装进这台机器的信任库 —— 浏览器会拦。装一次就够："
    Say  "      edge.bat trust"
    Say  "  别的设备：edge.bat ca"
    Write-Host ""
}

# 两级：先试全机（要提权），失败退回**只装当前用户**（不要提权，对单人开发机效果
# 一样，浏览器读的就是当前用户那本）。退回时必须**说清装的是哪一本** —— 以为装了
# 全机、换个账号又被拦，那种困惑比直接失败更费时间。
function Install-Trust {
    param($Caddy)
    & $Caddy trust
    if ($LASTEXITCODE -eq 0) {
        Ok "已装进**全机**信任库。**要重启浏览器。**"
        return
    }
    Warn "全机信任库装不进去（多半是没提权），改为只装当前用户……"
    if (-not (Test-Path $RootCrt)) {
        Die "还没有根证书 —— 先起一次网关（双击 edge.bat），让它把 CA 建出来。"
    }
    & certutil -user -addstore -f ROOT $RootCrt | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Die "两种都没装进去。手动来：右键 edge.bat → 以管理员身份运行 → edge.bat trust"
    }
    Ok "已装进**当前用户**的信任库（没提权，所以不是全机）。**要重启浏览器。**"
    Say "别的 Windows 账号仍会被拦；要全机生效就用管理员身份再跑一次 edge.bat trust"
}

function Export-Ca {
    if (-not (Test-Path $RootCrt)) { Die "还没有根证书 —— 先起一次网关：双击 edge.bat" }
    $dest = Join-Path $Here "root.crt"
    Copy-Item $RootCrt $dest -Force
    Ok "已导出 $dest（公钥，随便传）"
    Write-Host ""
    Say "别的 Windows（管理员）："
    Say "    certutil -addstore -f ROOT root.crt"
    Write-Host ""
    Say "Linux（curl / wget）："
    Say "    sudo cp root.crt /usr/local/share/ca-certificates/edge-root.crt"
    Say "    sudo update-ca-certificates"
    Write-Host ""
    Say "Linux 上的 Chrome / Chromium 另有一套信任库："
    Say '    certutil -d sql:$HOME/.pki/nssdb -A -t "C,," -n edge-root -i root.crt'
    Write-Host ""
    Say "装完**要重启浏览器**。"
}

#: Caddy 的 admin 端点。**它才是"已经在跑了"的真信号。**
#:
#: 撞车时 caddy 报的是这一句：
#:     listen tcp 127.0.0.1:2019: bind: Only one usage of each socket address...
#: 端口号是 2019 而不是 8443，没人会从它想到"网关已经起着了"——而那是最常见的
#: 原因（上一个窗口没关、后台 start 过一次）。所以起之前自己先问一句。
$AdminPort = 2019

function Test-AlreadyRunning {
    $null -ne (Get-NetTCPConnection -State Listen -LocalPort $AdminPort -ErrorAction SilentlyContinue)
}

# 已经在跑就说人话，不要把 caddy 那句 2019 的绑定错误丢给用户。
function Assert-NotRunning {
    if (-not (Test-AlreadyRunning)) { return }
    $who = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
    $line = if ($who) { "https://${EdgeHost}:$Port 已经在服务了。" } else { "但 $Port 上没人听——上一次可能没起干净。" }
    Write-Host ""
    Write-Host "[x] 网关已经在跑了。$line" -ForegroundColor Red
    Say "要停它：edge.bat stop"
    Say "要换配置：edge.bat reload（零中断，配置验不过会保留旧的）"
    Say "要看状态：edge.bat status"
    Write-Host ""
    Say "（caddy 自己的报错会说 `"listen tcp 127.0.0.1:$AdminPort`"——那是它的管理端点，"
    Say "  不是 $Port。端口号对不上，所以那句话指不到这里。）"
    exit 1
}

# ---------------------------------------------------------------------------
# 动作
# ---------------------------------------------------------------------------
if ($Verb -eq "get") { Get-Caddy; exit 0 }

$Caddy = Resolve-Caddy
if (-not $Caddy) {
    Die "找不到 caddy.exe。下载一次就够（约 45 MB，不进版本库）：`n`n        edge.bat get`n`n    或自己拿：https://caddyserver.com/download`n    存成 $LocalCaddy"
}

# EDGE_HOST 必须由这里传进去 —— Caddyfile 里的 {$VAR} 读的是**进程的环境变量**，
# 空的话站点地址退化成 https://:8443，证书签不出来而报错离根因很远。
$deploy = Resolve-Deployment (Join-Path $Here "..\..\.env")
$EdgeHost = $deploy.WebHost
$env:EDGE_HOST = $EdgeHost
Say "证书名 / 访问地址：$EdgeHost"
if ($deploy.AutoDetected) {
    Say "（.env 里 XINGCHA_WEB_HOST 没写死，这个 IP 是探出来的）"
}

switch ($Verb) {
    "run" {
        Assert-NotRunning
        Show-TrustHint
        Write-Host ""
        Say "https://${EdgeHost}:$Port  ->  $Upstream"
        Say "**关掉这个窗口就是停止。** 星槎自己要另外起（deploy\windows\xc.bat）。"
        Write-Host ""
        Say "第一次跑 Windows 防火墙会弹窗（它要监听 $Port）：**要允许，勾「专用网络」**。"
        Say "点了取消的话本机 https://127.0.0.1:$Port 完全正常，而别的机器一直连不上。"
        Write-Host ""
        & $Caddy run --config Caddyfile
        if ($LASTEXITCODE -ne 0) { Die "caddy 退出，原因在上面。" }
    }
    "start" {
        Assert-NotRunning
        Show-TrustHint
        & $Caddy start --config Caddyfile
        if ($LASTEXITCODE -ne 0) { Die "起不来，原因在上面。" }
        Ok "https://${EdgeHost}:$Port -> $Upstream（后台跑着，停止用 edge.bat stop）"
    }
    "stop" {
        if (-not (Test-AlreadyRunning)) {
            Warn "本来就没在跑。"
            exit 0
        }
        & $Caddy stop
        if ($LASTEXITCODE -ne 0) { Die "停不掉，原因在上面。" }
        Ok "已停止"
    }
    "reload" {
        & $Caddy reload --config Caddyfile
        if ($LASTEXITCODE -ne 0) { Die "配置验不过，已保留旧的，服务没下线。" }
        Ok "已重载（配置验不过会保留旧的，不会把服务弄下线）"
    }
    "trust" { Install-Trust -Caddy $Caddy }
    "ca" { Export-Ca }
    "status" {
        # 两个端口都看：只看 8443 的话，"进程还在但站点没起来"会被报成"没在跑"，
        # 而那时你去双击，撞上的是 2019 的绑定错误。
        $site = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
        if ($site) {
            Ok "网关在跑：https://${EdgeHost}:$Port"
        } elseif (Test-AlreadyRunning) {
            Warn "caddy 进程在（管理端点 $AdminPort 有人听），但 $Port 上没有站点。"
            Say  "  多半是上一次配置没加载成功。edge.bat stop 之后重来。"
        } else {
            Warn "$Port 没人听 —— 网关没起。双击 edge.bat"
        }
    }
    default {
        Die "不认识的动作：$Verb（get / run / start / stop / reload / trust / ca / status）"
    }
}
exit 0
