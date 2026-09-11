# 网关（Caddy 单文件）

**这个目录就是网关。** 一个可执行文件 + 一份 [`Caddyfile`](Caddyfile)，没有 docker、
没有容器网络、没有守护进程管理器。它和星槎跑在**同一台机器**上，反代
`127.0.0.1:8720`，对外只开 8443 一个 HTTPS 端口。

| 文件 | 是什么 |
|---|---|
| [`Caddyfile`](Caddyfile) | 配置。**Linux 与 Windows 共用同一份**，两条部署路径也共用 |
| [`edge`](edge) | Linux 上的薄脚本 |
| [`edge.bat`](edge.bat) | Windows 上的薄脚本，**可以直接双击** |
| `caddy` / `caddy.exe` | 二进制，`edge get` 现取。**不进版本库**（45 MB，且分平台） |

两个脚本只做三件事：认本目录的二进制（优先于 PATH，否则"我明明升级了"却跑着旧的
那个）、把 `EDGE_HOST` 传进进程、认准这一份 `Caddyfile` 而不是"你在哪儿敲的命令"。

---

## 起来

先确认仓库根 `.env` 里的 `XINGCHA_WEB_HOST` 是**这台机器的内网 IP**（不是
`0.0.0.0`，那是监听地址、不是能敲的名字）。它既是证书上的名字，也是你在浏览器里敲的
地址；两个脚本都从那儿读，不另设变量。

**Linux：**

```bash
./deploy/edge/edge get      # 下载 caddy 到这个目录（一次就够）
./deploy/edge/edge start    # 起来：https://<内网 IP>:8443 → 127.0.0.1:8720
./deploy/edge/edge trust    # 根证书装进本机信任库（会问密码），**要重启浏览器**
```

**Windows：**

```cmd
deploy\edge\edge.bat get
```

> Windows 这边是**一对文件**：`edge.bat` 是个纯 ASCII 的启动器，真正的逻辑与中文
> 输出都在同名的 `edge.ps1` 里。
>
> 分开是被逼的。cmd.exe 在 `chcp 65001` 下**按字节偏移回溯文件位置**，而偏移记账
> 按字符算——它会从一个汉字**中间**接着读，后半截字节落单（控制台显示成两个方块），
> 而**后半行被当成一条新命令执行**。这不是显示问题：`rem ... reset-password  忘了密码`
> 那行注释真的被跑过一次。而且它**时有时无**，只在文件不在系统页缓存里（刚改过、
> 刚开机）时才容易撞上，下一次又「好了」。
>
> 所以 `.bat` 里**一个非 ASCII 字节都不许有**，`.ps1` 反过来**必须带 UTF-8 BOM**
> （PowerShell 5.1 读无 BOM 的会按 ANSI 解码）。两条都有测试盯着，见
> `tests/test_deploy_artifacts.py`。

然后**双击 `edge.bat`**——窗口开着就是跑着，关掉就是停止，和 `deploy\windows\xc.bat`
同一个心智。

装根证书是**单独一步**（`edge.bat trust`），不会在起服务时偷偷发生：Caddyfile 里有
`skip_install_trust`。少了它，非管理员起服务时 Caddy 会去装全机信任库，弹一个 UAC
对话框然后**整个进程停在那里**——8443 一直没人听，日志最后一行是
`installing root certificate (you might be prompted for password)`，而没人会从这句话
想到"去点一下那个弹窗"。无人值守起服务时就是永久挂起。

`edge.bat trust` 会先试全机（要提权），失败退回**只装当前用户**（不要提权，对单人
开发机效果一样）。它会明说装的是哪一本。

> 第一次跑，Windows 防火墙会弹窗问要不要放行（它要监听 8443）：**要允许，勾「专用
> 网络」**。点了取消的话本机 `https://127.0.0.1:8443` 完全正常，而别的机器一直连不上
> ——那个现象指不到防火墙。
>
> 后台启动（`edge.bat start`）时那个弹窗**不一定出得来**，于是"允许"这一步被跳过而你
> 不知道。别的设备连不上时先查这两样（都要管理员）：
>
> ```powershell
> # 1. 网络类型。「公用」下防火墙最严，改成「专用」
> Get-NetConnectionProfile
> Set-NetConnectionProfile -InterfaceAlias WLAN -NetworkCategory Private
>
> # 2. 放行 8443 入站
> New-NetFirewallRule -DisplayName 'xingcha edge 8443' -Direction Inbound -Protocol TCP -LocalPort 8443 -Action Allow -Profile Private
> ```

## 证书上的名字从哪来

`XINGCHA_WEB_HOST`（仓库根的 `.env`）。它**只决定证书上的名字与你在浏览器里敲的地址**，
不决定绑哪个网口——那是 Caddyfile 里的 `bind 0.0.0.0`。

留空或填 `0.0.0.0` 时，脚本会**自动探测本机内网 IP**（取有默认网关、网卡 Up 的那一张，
跳过 VMware / VirtualBox / WSL 那些别人到不了的虚拟网卡）。给局域网用时可以不填，
DHCP 换地址也不用改。

> 证书签不给 `0.0.0.0`，浏览器里也没人敲它。写死 IP 的代价是换一次地址就"证书警告点
> 不过去"，而 `.env` 看起来完全正常。

## 然后星槎那边

| 星槎怎么跑 | 要做的 |
|---|---|
| 有 docker | `.env` 里 `XINGCHA_GATEWAY=edge`，然后 `./deploy/linux/xc start` |
| 没有 docker | `.env` 最后一节放开 `XINGCHA_TRUSTED_PROXIES=127.0.0.1` 与 `XINGCHA_PUBLIC_URL`，然后照常起 |

两条路的结果一样：星槎监听 `127.0.0.1:8720`，**局域网上连不到**。docker 那条由
[`../linux/xc`](../linux/xc) 的 `derive_bind_addr` 强制绑回环，不受 `XINGCHA_WEB_HOST`
影响；变量注释在 [`../.env.example`](../.env.example)。

`xc start` 会先看 8443 上有没有人听——**网关是硬依赖**，它没起后台完全进不去，
而那时容器是 healthy 的。

> `reverse_proxy` 里的端口是写死的 `8720`。改了 `.env` 的 `XINGCHA_WEB_PORT` 就要
> 同步改这里，否则网关回 502 而星槎自己完全健康。

## 日常

| 命令 | 做什么 |
|---|---|
| `edge get` | 下载 caddy（一次就够；换版本先删掉那个文件） |
| `edge start` / `edge run` | 后台起 / 前台起（前台看得见日志，Ctrl-C 停） |
| `edge stop` | 停 |
| `edge reload` | 零中断重载配置。**验不过会保留旧配置**，不会让所有站点一起躺下 |
| `edge trust` | 把根证书装进**本机**信任库（Windows 上全机失败会退回当前用户） |
| `edge ca` | 导出 `root.crt` + 印出别的设备（手机 / Windows / 另一台 Linux）怎么装 |
| `edge status` | 8443 上有没有人在听 |

Windows 上把 `edge` 换成 `edge.bat`，动词一样。

---

## 装根证书

内网只有 IP、没有公网域名，拿不到 Let's Encrypt（公网 CA 不给私网 IP 签）。Caddy 用
自己的**内部 CA** 签，而浏览器不认识那个 CA。两档差别很大，**不装等于只买到一半**：

| | 防被动嗅听 | 防主动中间人 |
|---|---|---|
| 装了根证书 | ✅ | ✅ 浏览器静默信任，换不了证书 |
| 每次点"继续前往" | ✅ | ❌ 攻击者递一张自签证书，你同样会点过去 |

本机一条 `edge trust`。别的设备跑 `edge ca`：导出 `root.crt`（公钥，随便传）并印出
各系统的安装命令与指纹。装完**都要重启浏览器**。

> Linux 上 Chrome / Chromium 用的是**另一套信任库**（NSS）。只装了系统那处的症状最
> 迷惑：`curl` 不加 `-k` 就通了，而浏览器照旧拦——看起来像浏览器坏了。缺 `certutil`
> 时先 `sudo apt install -y libnss3-tools`，再跑一次 `edge trust`。

> **CA 私钥别删也别外传**：Linux 在 `~/.local/share/caddy`，Windows 在
> `%AppData%\Caddy`。删了会生成一个新 CA，你装过的所有设备会一起开始报证书错误。

---

## Caddyfile 里那几行，少一行各有各的坑

| 那一行 | 少了会怎样 |
|---|---|
| `default_sni` | 按 IP 访问时客户端**不发 SNI**（RFC 6066 不允许 IP 出现在 SNI 里），Caddy 一张证书都选不出来，TLS 直接回 `alert internal error`（curl exit 35 / 浏览器"无法建立安全连接"）。**最关键的一行**，而且它是全局选项——写成 `tls` 的子指令会报 `unknown subdirective` |
| `bind 0.0.0.0` | 站点地址里的字面 IP 会让 Caddy **绑到那个 IP**，DHCP 换一次地址就起不来（`cannot assign requested address`）。地址里的 IP 只用来签证书与匹配 |
| `tls internal` | 会去公网 ACME 给一个私网 IP 签证书，必然失败并反复重试 |
| `flush_interval -1` | SSE 被缓冲，症状是**回答要等全部生成完才一次性蹦出来**，而接口本身完全正常 |
| 脚本没传 `EDGE_HOST` | Caddyfile 里的 `{$VAR}` 读的是**进程的环境变量**，空的话站点地址退化成 `https://:8443`，证书签不出来而报错离根因很远 |

还有一条是**别加 HSTS**（别写 `Strict-Transport-Security`）：内网这里零收益，代价是
浏览器会**拒绝**你点"继续前往"，而新设备在装根证书之前必然撞证书警告——它把"点一下
继续"变成"这个站点你今天进不去了"，而且没有任何提示说原因是一个响应头。

> `http://<IP>:8443`（对着 HTTPS 端口说明文）是 `400 Bad Request`。Caddy 不会在同一个
> socket 上兼容两种协议，那是 TLS 握手失败的正常结果，不是配置问题。

改完配置：`edge reload`（零中断，验不过保留旧配置）。

---

## 多个项目共用这一台

`Caddyfile` 末尾有一段注释掉的模板：照抄、换端口、换 `reverse_proxy` 目标，
`edge reload`。证书与 CA 是同一份，所以**根证书仍然只在每台设备装一次**——这就是
"一台机器一个网关"的全部意义。

按**端口**分流而不是主机名：内网往往没有 DNS，而 TLS 的 SNI 也不允许装 IP。
还没起来的站点回 502，不影响别的项目。

别的项目也要把端口**绑在 `127.0.0.1` 上**（docker 是 `ports: - "127.0.0.1:8000:8000"`）。
绑到 `0.0.0.0` 就是在 TLS 旁边另开一条明文入口，而且 docker 发布的端口走 `DOCKER-USER`
链、**绕过 ufw**。
