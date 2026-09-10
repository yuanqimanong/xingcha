# 网关部署（Caddy）

> **`edge` 就是这台 Caddy。** 目录、docker 网络、`.env` 里的 `XINGCHA_GATEWAY`
> 用的都是这个名字。取角色名而不是产品名，是为了哪天换成 nginx / Traefik 时
> 目录与配置不用跟着改。
>
> 网关是一个**独立项目**，代码在 `../../edge/`（与 xingcha 平行的目录）。
> 这份文档是它唯一的部署说明——xingcha 自己的部署见 [README.md](README.md)。

**一台 Caddy，服务本机上的所有项目。** 与各项目的部署脚本完全分离——这里不碰任何
项目，各项目也不需要知道 Caddy 的存在。

## 一条命令

```bash
cd ../edge && cp .env.example .env && $EDITOR .env && ./edge start
```

`.env` 里只有一项必须改：`EDGE_HOST` —— 你在浏览器里敲的主机名或 IP。

然后**在本机装根证书**（要 sudo）：

```bash
sudo apt install -y libnss3-tools   # Chrome/Chromium 用自己的信任库，需要 certutil
./edge trust
```

`./edge trust` 装两处：系统信任库（curl / wget 用）与 `~/.pki/nssdb`
（Chrome / Chromium 用），装完会**逐处自检并报出结果**。**要重启浏览器。**

> 两处用的是**不同的信任源**。只成了系统那一处的症状最迷惑：
> `curl` 不加 `-k` 就通了，而浏览器照旧拦——看起来像浏览器坏了。
> 所以 `certutil` 缺失时必须先装它再重跑，`trust` 会把这句话说出来。

别的设备（手机 / Windows / macOS）跑 `./edge ca`，它会打印各系统的安装命令。

> 这一步不是可选的。不装的话浏览器每次都拦，而点"继续前往"只防被动嗅听——
> 理由见下一节。

---

## 为什么要装根证书

内网只有 IP、没有公网域名，所以拿不到 Let's Encrypt 的证书（公网 CA 不给私网 IP
签）。Caddy 用自己的**内部 CA** 签，而浏览器不认识那个 CA。

于是有两档，差别很大：

| | 防被动嗅听 | 防主动中间人 |
|---|---|---|
| 装了根证书 | ✅ | ✅ 浏览器静默信任，换不了证书 |
| 每次点"继续前往" | ✅ | ❌ 攻击者递一张自签证书，你同样会点过去 |

**不装等于只买到一半。** 而"一台 Caddy = 一个 CA"的全部意义就是这件事**只做一次**，
之后所有项目都被信任。每个项目自带一个 Caddy 就是 N 个 CA、装 N 次——那种事没人会
坚持做，最后大家都在点"继续前往"。

`./edge ca` 会打印指纹，装完可以核对。

> `root.crt` 是公钥，随便传；私钥在 `caddy_data` 卷里，**别导出、别外传**。
> **`caddy_data` 卷不能删**——删了会生成一个新的 CA，你装过的所有设备会一起开始
> 报证书错误。

---

## 拓扑

```
                    ┌─────────────────────────────┐
   浏览器 ──HTTPS──▶│ edge-caddy-1                │
                    │  :8443 → xingcha:8720       │
                    │  :9443 → fin:8000           │
                    │  :9444 → pyp:8000           │
                    └──────────┬──────────────────┘
                               │ docker 网络 edge（明文，但不出主机）
                 ┌─────────────┼─────────────┐
                 ▼             ▼             ▼
            xingcha          fin           pyp
         （零宿主端口）  （零宿主端口）  （零宿主端口）
```

按**端口**分流，不按主机名——内网往往没有 DNS，而 TLS 的 SNI 也不允许装 IP。
各项目零宿主端口，于是也就不再有"映射出去的端口绕过 ufw"这个问题。

另外 80 端口兜一个 **HTTP → HTTPS 跳转**：裸着敲 `192.168.1.10`（没协议、没端口）
会落在那里，然后 308 跳到 xingcha 的 HTTPS 地址，路径保留。

> `http://<ip>:8443`（对着 HTTPS 端口说明文）仍然是 `400 Bad Request`。
> **Caddy 不会在同一个 socket 上兼容两种协议**——`auto_https` 与 `https_port`
> 都试过，都不改变这一点。那是 TLS 握手失败的正常结果，不是配置问题。

Caddy → 应用这一跳是明文，但它**留在同一台主机内部**，不经过网络。
跨机器接入（比如 fin 跑在 Windows 那台）会让这一跳变成跨网络的明文——见最后一节。

---

## 接一个项目进来

```bash
./edge join ../finance-data-crawler   # 打印要改什么
```

项目侧要做两件事：加入外部网络 `edge`、**不再发布宿主端口**。

```yaml
networks:
  default:
    name: edge
    external: true

services:
  <服务名>:
    # ports: 删掉
    expose:
      - "<容器内端口>"
```

然后把 `Caddyfile` 里对应站点的 `reverse_proxy` 目标改成 `<服务名>:<端口>`，
最后 `./edge reload`（零中断：配置验不过会保留旧配置，不会让所有项目一起躺下）。

**xingcha 已经备好**，在它的 `.env` 里打开开关即可：

```bash
XINGCHA_GATEWAY=edge          # 留空则独立跑、明文 HTTP，不经网关
XINGCHA_GATEWAY_PORT=8443     # 网关上分给它的端口
```

```bash
cd ../xingcha && ./deploy/linux/xc start
```

`xc` 自己读这一项决定叠不叠 `deploy/linux/docker-compose.gateway.yml`，并且**只在配了
网关时才检查它在不在**——没配就不该被一个不相干的容器挡住。

还没接进来的站点会回 **502**，不会拖垮网关——一个项目没上线不该影响别的项目。

### 反代后面的应用要信任 `X-Forwarded-Proto`

否则应用以为自己在 http 上，**会话 cookie 不带 `Secure`**：浏览器那半段明明是
HTTPS，却少了一层保护，而功能完全正常，没人会注意到。

xingcha 的叠加层里已经设了 `XINGCHA_TRUSTED_PROXIES=*`。敢用 `*` 的理由很具体：
那个容器**零宿主端口**，唯一入口就是这个网关。默认部署里它是空的，也就是谁都不信。

---

## 日常

| 命令 | 做什么 |
|---|---|
| `./edge start` | 一条命令搞定（幂等，随时可重跑） |
| `./edge trust` | 把根证书装进**本机**信任库（系统 + Chrome 的 NSS，要 sudo），并自检两处 |
| `./edge ca` | 只重新导出根证书 + 打印别的系统怎么装 |
| `./edge routes` | 当前路由 + 哪些容器已接入 |
| `./edge reload` | 零中断重载 Caddyfile |
| `./edge logs [n]` | 跟随日志 |
| `./edge stop` | 停止，证书卷保留 |

---

## 四个坑（都实际踩过）

**0. 别给内网网关发 HSTS。**
它在这里零收益（80 端口根本没发布，不存在可降级的 http 入口），代价却很实：
HSTS 会让浏览器**拒绝**你点"继续前往"，而这套用的是内部 CA，新设备在装根证书
之前必然撞证书警告。于是它把"点一下继续"变成"这个站点你今天进不去了"，
而且没有任何提示说原因是一个响应头。


**1. Caddyfile 里的 `{$VAR}` 读的是容器里的环境变量，不是 compose 的插值。**
compose 必须显式 `environment:` 传进去。曾经 Caddyfile 引用 `{$ACME_EMAIL}` 而
compose 从没传过 → 容器里为空 → `email` 零参数 → 配置解析失败 → 无限重启循环，
而日志里的报错离根因很远。

**2. 站点地址里写字面 IP 会让 Caddy 绑到那个 IP。**
在容器里那通常是容器自己的回环，而宿主的端口映射转发到容器 eth0，于是永远到不了。
症状是 curl 返回 000，日志却显示 "server running" 且证书已签发成功。
所以地址里的 IP 只用来签证书与匹配，实际监听交给 `bind 0.0.0.0`。

**3. 按 IP 访问时客户端不发 SNI，Caddy 就一张证书都选不出来。**
RFC 6066 不允许 IP 出现在 SNI 里。没有 SNI 时 Caddy 默认无从挑选，TLS 直接回
`alert internal error`（curl exit 35 / 浏览器"无法建立安全连接"）。
解法是全局选项 **`default_sni`**（不是 `tls` 的子指令——写成后者会报
`unknown subdirective`）。这是整份 Caddyfile 里最关键的一行。

---

## 跨机器接入（比如 Windows 那台）

**别的机器上的程序加入不了这个 docker 网络**——docker 网络不跨主机。做法是让它
发布自己的端口，网关按 `IP:端口` 反代：

```
https://{$EDGE_HOST}:9445 {
	import common
	reverse_proxy 192.168.x.y:3000 { flush_interval -1 }
}
```

浏览器只访问网关地址，证书是网关的，那台机器的 IP 从不出现在浏览器里。

**但 Caddy → 应用这一跳会变成跨网络的明文**，也就是"浏览器那半段加密、网络这半段
不加密"。别在这种情况下以为 TLS 是端到端的。另外网关会成为硬依赖：Windows 那台
好着、网关那台挂了，fin 就打不开。

### xingcha 在 Windows 那台时也走这条

那边**不打镜像**——`deploy\windows\xc.bat` 用 uv 直接在宿主起进程，理由见
[README.md 的 Windows 一节](README.md#windows不走-docker)。于是它也加入不了 `edge`
网络，走的就是上面这套。

**不用新加站点块**：xingcha 原本那一段照旧，只把 `reverse_proxy` 的目标从容器名
换成那台机器的「内网 IP:8720」。端口分流、证书、`{$EDGE_HOST}` 全都不变。

```
	reverse_proxy xingcha:8720        →  reverse_proxy 192.168.x.y:8720
```

`import common`（或那一段里等价的 `flush_interval -1`）**要保留**：少了它 SSE
流式响应会被缓冲，症状是"回答要等全部生成完才一次性蹦出来"，而接口本身完全正常。

Windows 那侧要对应放开 `.env` 最后一节的三项——`XINGCHA_HOST=0.0.0.0`、
`XINGCHA_PUBLIC_URL`、`XINGCHA_TRUSTED_PROXIES=<网关 IP>`。少一项各有各的坑法，
表在 [README.md](README.md#https-仍然是-linux-那台-caddy-给的)。最后一项别照抄
容器那边的 `*`：那边敢信任所有来源的前提是零宿主端口，而这里端口是真的开在
局域网上的。

想跨机器又不要明文跳：Caddy 支持 `acme_server`，让这台网关同时充当**内网 ACME
CA**，Windows 那台再跑一个小 Caddy 从它签证书。这样每台机器本地终止 TLS，
而根证书仍然只有一份、只装一次。代价是多一层配置。
