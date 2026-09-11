<#
.SYNOPSIS
    两个 Windows 脚本共用的推导：读 .env，算出「地址怎么来」的那几项。

.DESCRIPTION
    ``xc.ps1`` 与 ``edge.ps1`` 都要回答同一组问题——证书签给谁、应用绑哪儿、后台
    印什么地址。答案必须一致，否则症状是"页面上的地址打不开，而服务本身好着"。

    **用户只填三项**（见 deploy/.env.example 第 1 节）：

        XINGCHA_GATEWAY     前面有没有那个 Caddy
        XINGCHA_WEB_HOST    你在浏览器里敲的名字（空 / 0.0.0.0 = 自动探测内网 IP）
        XINGCHA_WEB_PORT    不挂网关时容器/进程对外的端口

    其余四项（``XINGCHA_HOST`` / ``XINGCHA_PORT`` / ``XINGCHA_PUBLIC_URL`` /
    ``XINGCHA_TRUSTED_PROXIES``）**是推出来的，不该出现在 .env 里**。

    以前它们要用户自己填，代价有两个：一是 ``XINGCHA_WEB_HOST`` 和
    ``XINGCHA_HOST`` 名字像、作用完全不同（前者应用根本不读），填错是静默的；
    二是"挂了网关"这件事要在四个地方分别表达一遍，只改一处的后果同样安静
    ——比如 PUBLIC_URL 写了 https 而 TRUSTED_PROXIES 没配，于是会话 cookie
    不带 Secure，而功能完全正常。

    规则与 ``deploy/linux/xc`` 的 ``derive_bind_addr`` / ``public_port`` 逐条对齐：
    两条路推出来的东西必须一样，否则"我在 Windows 上跑的和 Linux 上不是一个东西"。

    **本文件必须存成带 BOM 的 UTF-8**（PowerShell 5.1 读无 BOM 的按 ANSI 解码）。
#>

#: 网关的 HTTPS 端口。真值在 deploy/edge/Caddyfile 里，这里只是照抄一份给脚本用。
#: 有一条测试盯着两处相等——写两遍就会不一致，而不一致的症状是后台印出一个打不开
#: 的地址、服务本身却好着。
$script:GatewayPort = 8443

#: 不挂网关时应用的默认端口。
$script:DefaultPort = 8720


function Read-DotEnv {
    <#
    .SYNOPSIS
        读 .env 成一个哈希表。注释行与空行跳过，值两边的引号剥掉。
    #>
    param([string]$Path)

    $out = @{}
    if (-not (Test-Path $Path)) { return $out }
    foreach ($line in Get-Content $Path) {
        $s = $line.Trim()
        if (-not $s -or $s.StartsWith("#") -or -not $s.Contains("=")) { continue }
        $i = $s.IndexOf("=")
        $k = $s.Substring(0, $i).Trim()
        $v = $s.Substring($i + 1).Trim().Trim('"').Trim("'")
        if ($k) { $out[$k] = $v }
    }
    return $out
}


function Find-LanIp {
    <#
    .SYNOPSIS
        本机内网 IP。探不到返回 $null。

    .DESCRIPTION
        只认有默认网关、且网卡 Up 的那一张：VMware / VirtualBox / WSL 的虚拟网卡
        同样有地址、同样 Up，但局域网里的别人根本到不了它们。
    #>
    try {
        $cfg = Get-NetIPConfiguration |
            Where-Object { $_.IPv4DefaultGateway -and $_.NetAdapter.Status -eq 'Up' } |
            Select-Object -First 1
        if ($cfg) { return $cfg.IPv4Address.IPAddress }
    } catch { }
    return $null
}


function Resolve-Deployment {
    <#
    .SYNOPSIS
        读 .env，返回一个对象：WebHost / Gateway / Port / PublicUrl / BindAddr /
        TrustedProxies / AutoDetected。

    .DESCRIPTION
        调用方只管用，不要自己再算一遍——**这就是"一处定义"的那一处**。
    #>
    param([string]$EnvPath)

    $env_ = Read-DotEnv $EnvPath
    $gateway = $env_["XINGCHA_GATEWAY"]
    $webHost = $env_["XINGCHA_WEB_HOST"]
    $webPort = $env_["XINGCHA_WEB_PORT"]
    if (-not $webPort) { $webPort = $script:DefaultPort }

    # 绑哪儿。**只看用户写的原值**，不看下面探测出来的那个——顺序很要紧。
    #
    # 此前是先探测、再拿探测结果去判断，于是 WEB_HOST 留空会落进 else 绑上
    # 0.0.0.0：同一份 .env 在 Linux 上只绑回环，在 Windows 上却把明文 HTTP 开给了
    # 整个局域网。而"开给局域网"按设计必须是**一次显式选择**（把 WEB_HOST 填成真
    # 地址或 0.0.0.0），留空不是。
    #
    # 三条规则，第一条优先——与 deploy/linux/xc 的 derive_bind_addr 逐条对齐：
    #
    #   挂网关                  → 127.0.0.1。网关是本机进程，走回环就够了；绑到局域网
    #                             上等于在 TLS 旁边另开一条明文入口，而两条入口并存时
    #                             人只会记住能打开的那一个。
    #   留空 / localhost / 回环 → 127.0.0.1
    #   其它（含显式 0.0.0.0）  → 0.0.0.0
    #
    # 不直接绑那个 IP：更精确，但 DHCP 换一次地址就起不来
    # （cannot assign requested address），而那个报错离"我改了个显示用的字段"很远。
    $bind = if ($gateway) {
        "127.0.0.1"
    } elseif ($webHost -in @("", "localhost", "127.0.0.1")) {
        "127.0.0.1"
    } else {
        "0.0.0.0"
    }

    # 名字。0.0.0.0 / :: 是**监听地址，不是能敲的主机名**：证书签不给它们，浏览器里
    # 也没人敲。所以不拒绝，替他探一个内网 IP 出来——写死 IP 的话 DHCP 换一次地址就
    # "证书警告点不过去"，而 .env 看起来完全正常。
    #
    # **只在那个地址真的对外可达时才探。** 绑在回环上却印一个内网 IP，就成了
    # "页面上的地址看起来完全正确，而别人就是打不开"——这一路上最难认的症状。
    # 挂网关时可达性来自 Caddy（它自己绑 0.0.0.0:8443），所以那时照探。
    $auto = $false
    if ($webHost -in @("", "0.0.0.0", "::", "[::]")) {
        if ($gateway -or $bind -eq "0.0.0.0") {
            $webHost = Find-LanIp
            if ($webHost) { $auto = $true } else { $webHost = "localhost" }
        } else {
            $webHost = "localhost"
        }
    }

    # 对外端口与地址。挂网关时端口是**网关的**（8443），不是应用的。
    $publicPort = if ($gateway) { $script:GatewayPort } else { $webPort }
    $scheme = if ($gateway) { "https" } else { "http" }

    [PSCustomObject]@{
        Gateway    = [bool]$gateway
        WebHost    = $webHost
        # 探测来的还是用户写死的。调用方要据此决定说什么话。
        AutoDetected = $auto
        Port       = $webPort
        BindAddr   = $bind
        PublicUrl  = "${scheme}://${webHost}:${publicPort}"
        # 挂网关时 Caddy 从回环连过来，要信它的 X-Forwarded-Proto，否则应用以为
        # 自己在 http 上、**会话 cookie 不带 Secure**——功能完全正常，只是少一层
        # 保护，没人会注意到。只信回环：那个头能伪造，信 * 等于谁都能伪造。
        TrustedProxies = if ($gateway) { "127.0.0.1" } else { "" }
    }
}
