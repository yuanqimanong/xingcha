# 部署

按这台机器有没有 docker 分三条路。三条共用同一个网关、同一份 `.env` 模板、同一个
`data/` 位置。

| 机器 | 怎么跑 | 起停 |
|---|---|---|
| [Linux 无 docker](#一--linux-无-docker) | `uv run --frozen --no-dev xingcha serve` | 关掉终端就是停 |
| [Linux 有 docker](#二--linux-有-docker) | `./deploy/linux/xc start` | `restart: unless-stopped`，开机自启 |
| [Windows](#三--windows) | 双击 `deploy\windows\xc.bat` | 关掉窗口就是停 |

要 HTTPS 或让别的机器访问，**先起[网关](#网关)，再起星槎**。

---

## 一 · Linux 无 docker

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # 一次性。装完**重开一个窗口**，PATH 在进程启动时读
uv run --frozen --no-dev xingcha serve            # 127.0.0.1:8720
```

`uv run` 会先按 `uv.lock` 对齐环境再跑，不需要单独 `uv sync`；Python 也由 uv 自己装。
`.env` 不用先复制，没有它就全走默认值（只有本机能开）。

**这条路不看 `XINGCHA_GATEWAY` / `WEB_PORT` / `DATA_MOUNT`**——那三项是给 compose 的。
应用读的是 `XINGCHA_HOST` / `PORT` / `PUBLIC_URL` / `TRUSTED_PROXIES`。

要局域网访问，二选一：

```bash
# A. HTTPS（推荐）：起网关，星槎仍绑回环，.env 里加两行
XINGCHA_TRUSTED_PROXIES=127.0.0.1
XINGCHA_PUBLIC_URL=https://<本机内网 IP>:8443

# B. 明文 HTTP：端口真的开在局域网上，后台密码与 sk-xc- 裸传
uv run --frozen --no-dev xingcha serve --host 0.0.0.0
```

A 里那两项少一项都不报错：不填 `TRUSTED_PROXIES`，应用以为自己在 http 上，**会话
cookie 不带 `Secure`**；不填 `PUBLIC_URL`，后台印出的是 `http://127.0.0.1:8720`，复制走的
curl 在别的机器上必然连不上。只填 `127.0.0.1` 不填 `*`：那个头能伪造，只信回环等于只有
已经在这台机器上的进程才伪造得了。

B 是一次显式选择，选了就别再起网关——两条入口并存时人只会记住能打开的那一个。

关掉终端就是停止，开机自启要自己写 systemd 单元。日常动作直接敲，没有包装：

```bash
uv run xingcha doctor / admin status / admin reset-password / db backup / db verify
```

## 二 · Linux 有 docker

前置：`docker` 与 `docker compose` v2（v1 的 `docker-compose` 已 EOL）。缺什么 `xc` 会给出
可直接粘贴的安装命令。

```bash
# .env 里写 XINGCHA_GATEWAY=edge，然后
./deploy/edge/edge start    # 网关
./deploy/linux/xc start     # 首次会从 deploy/.env.example 生成 .env 并停下来提示填写
```

| 命令 | 做什么 |
|---|---|
| `xc start` | 重新构建并启动，**data 一个字节都不动** |
| `xc update` | 拉代码 + 重新构建启动（工作区脏时拒绝，不会 `reset --hard`） |
| `xc redeploy` | 连数据一起清空（会问一次 `yes`） |
| `xc stop` / `logs [n]` / `status` | 停止（data 保留）／跟随日志／容器与账号状态 |

**一律走 `xc`，不要手敲 compose**：它替你传好 `-f` 与 `--env-file`、启动前检查网关在不在，
并挡住三个踩过的坑——容器还在跑时删 `data/`（进程握着已删除的 inode 继续写）、删完
Docker 用 root 重建挂载点（容器里 UID 10001 写不进去，直接重启循环）、`COMPOSE_FILE` 那种
"在哪个目录敲命令会改变结果"的配置。

挂网关时星槎那个端口**被强制只绑 `127.0.0.1`**（`xc` 的 `derive_bind_addr`，不受
`XINGCHA_WEB_HOST` 影响），唯一进得来的就是本机那个 Caddy。**网关因此是硬依赖**：它没起，
后台完全进不去——包括进去修东西，而那时容器是 healthy 的。

不挂网关时那个端口是明文 HTTP，且 docker 发布的端口走 `DOCKER-USER` 链、**绕过 ufw**：
即使防火墙写了 deny，映射出去的端口照样可达。

升级、回滚、密钥环轮换这些要对容器说话的动作在仓库根的 [README](../README.md#运维)。

## 三 · Windows

双击 `deploy\windows\xc.bat`：确认有 `uv` → 没有 `.env` 就从**同一份**模板生成 →
`uv sync --frozen --no-dev` → `uv run xingcha serve`。窗口开着就是跑着，关掉就是停止。
更新是 `git pull` 之后再双击一次。

要 HTTPS / 局域网访问，再双击 `deploy\edge\edge.bat` 起网关，并在 `.env` 里手写第一节
那两项（`XINGCHA_TRUSTED_PROXIES=127.0.0.1`、`XINGCHA_PUBLIC_URL=https://<内网 IP>:8443`）。
**`XINGCHA_HOST` 不用动**——Caddy 就在同一台机器上，改成 `0.0.0.0` 等于在局域网上多开一条
绕过 TLS 的明文入口。

三个会卡住人的地方：

- **防火墙弹窗**问的是 `caddy.exe`（监听 8443）和 `python.exe`，要允许且要勾「专用网络」。
  点取消的话本机完全正常、别的机器一直连不上——那个现象指不到防火墙。
- **`uv` 装完要重开窗口**，PATH 是进程启动时读的。
- **双击一闪就没**：从 cmd 里跑一次 `deploy\windows\xc.bat` 看报错。

日常动作同第一节（`uv run xingcha doctor` 等）。`deploy/linux/xc` 与 `drill.sh` 整个是
docker 包装，这条路上不适用。

**Windows 上不打镜像不是图省事**：Docker Desktop 经 9p/virtiofs 把 Windows 目录挂进虚拟机，
那是网络文件系统，**SQLite 的 WAL 在上面会静默降级**——症状是零星的 `database is locked`，
只在并发写时出现，压不出来也难复现。绕开只能改用命名卷，于是备份、`db verify`、恢复演练
全都得进容器做。

`xc.bat` / `edge.bat` 都只是启动器，逻辑在同名的 `.ps1` 里。分开是被逼的：cmd.exe 在
`chcp 65001` 下按字节偏移回溯文件位置，会从一个汉字中间接着读，**后半行被当成一条新命令
执行**（`rem ... reset-password 忘了密码` 那行注释真的被跑过一次），而且时有时无。所以
`.bat` 里**一个非 ASCII 字节都不许有**，`.ps1` 反过来**必须带 UTF-8 BOM**（PowerShell 5.1 读
无 BOM 的按 ANSI 解码）。两条都有测试盯着，见 `tests/test_deploy_artifacts.py`。

---

## 网关

`deploy/edge/` 就是网关：**一个可执行文件 + 一份 `Caddyfile`**，没有 docker。它和星槎跑在
**同一台机器**上，反代 `127.0.0.1:8720`，对外只开 8443 一个 HTTPS 端口。`Caddyfile` 三条路
共用，每一行为什么少不了写在它自己的注释里。

起之前先确认仓库根 `.env` 的 `XINGCHA_WEB_HOST` 是**这台机器的内网 IP**——它既是证书上的
名字，也是你在浏览器里敲的地址，两个脚本都从那儿读，不另设变量。填 `0.0.0.0` 或留空时
脚本会自动探测（取有默认网关、网卡 Up 的那张，跳过 VMware / VirtualBox / WSL 的虚拟网卡）。

| 命令 | 做什么 |
|---|---|
| `edge get` | 下载 caddy 到这个目录（约 45 MB，一次就够，不进版本库） |
| `edge start` / `run` | 后台起 / 前台起（看得见日志，Ctrl-C 停） |
| `edge stop` / `status` | 停 ／ 8443 上有没有人在听 |
| `edge reload` | 零中断重载配置。**验不过会保留旧配置**，不会让所有站点一起躺下 |
| `edge trust` | 把根证书装进**本机**信任库（会问密码），**装完要重启浏览器** |
| `edge ca` | 导出 `root.crt` + 印出别的设备（手机 / Windows / 另一台 Linux）怎么装 |

Linux 是 `./deploy/edge/edge <动词>`，Windows 双击 `edge.bat` 或 `edge.bat <动词>`，动词一样。

**根证书每台设备装一次。** 内网只有 IP、没有公网域名，公网 CA 不给私网 IP 签，所以用
Caddy 自己的内部 CA，而浏览器不认识那个 CA。不装的话只能一路点"继续前往"，那**只防被动
嗅听**——主动中间人递一张自签证书你同样会点过去。两个坑：Firefox / Chromium 用的是另一套
信任库（NSS），只装系统那处的症状最像"我明明装了"，Linux 上 `sudo apt install -y libnss3-tools`
再跑一次 `edge trust`；CA 私钥在 `~/.local/share/caddy`（Windows `%AppData%\Caddy`），**删了会
生成一个新 CA**，你装过的所有设备会一起开始报证书错误。

装根证书是**单独一步**，不会在起服务时偷偷发生（`Caddyfile` 里有 `skip_install_trust`）：
少了它，非管理员起服务时 Caddy 会去装全机信任库，弹一个 UAC 对话框然后**整个进程停在
那里**，8443 一直没人听，日志最后一行看不出要去点那个弹窗。

两条别忘的：改了 `XINGCHA_WEB_PORT` 就要同步改 `Caddyfile` 里 `reverse_proxy` 的端口，否则
网关回 502 而星槎自己完全健康；**TLS 必须在同一台机器上终止**，把 Caddy 挪到另一台去反代的话，
浏览器那半段仍是 HTTPS，而「Caddy → 星槎」那半段变成跨网络明文——最容易被误当成端到端加密
的一种拓扑。

这台机器上的**别的项目**：照抄 `Caddyfile` 里那段注释、换个端口即可，证书与 CA 是同一份，
根证书仍然只装一次。按端口分流而不是主机名：内网往往没有 DNS，而 TLS 的 SNI 也不允许装 IP。

## `.env`

完整注释见 [`.env.example`](.env.example)，复制到**仓库根**（不是 `deploy/` 下）。**一项都不改
也能起来。** 四项最常动的：

| 变量 | 默认 | 说明 |
|---|---|---|
| `XINGCHA_GATEWAY` | 空 | 空 = 自己发布端口、明文 HTTP；`edge` = 挂到本机那个 Caddy 上。**只对 docker 那条路生效** |
| `XINGCHA_WEB_HOST` | `localhost` | 你在浏览器里敲的主机名或 IP，也是证书上的名字 |
| `XINGCHA_WEB_PORT` | `8720` | **独立跑时**发布的宿主端口；走网关时你敲的是网关的 8443 |
| `XINGCHA_ADMIN_PASSWORD` | 空 | 留空 = 首次访问 `/admin` 引导设定 |

`XINGCHA_WEB_HOST` 同时决定 docker 那条路绑哪个接口（`localhost` → 只绑回环，其它 → `0.0.0.0`）。
合成一个变量是因为分成两个最容易出的错是二者对不上：页面上显示局域网 IP、实际只绑了回环，
于是别人打不开而那个地址看起来完全正确。

> **它和应用自己 bind 的 `XINGCHA_HOST` 不是一回事**——应用根本不读 `WEB_HOST`（当未知项忽略）。
> 想"让别的机器能访问"而只改了它：docker 那条路有效，**uv 直跑那条是个静默空操作**。

## 初始化

打开 `https://<内网 IP>:8443/admin`（不挂网关时是 `http://<地址>:<端口>/admin`）。

1. 设管理员密码（至少 12 位，别复用其它服务的——这个后台能改写上游 `base_url`）。也可以在
   `.env` 里预设 `XINGCHA_ADMIN_PASSWORD`。**先立者为准**：库里一旦有了密码，那一项就被忽略，
   这样任何能往 `.env` 写一行的人都顶不掉已建好的密码。忘了密码走 `xingcha admin reset-password`。
2. 「上游」页填 key。
3. 「密钥」页签发一把 `sk-xc-`，交给业务代码。

```bash
curl https://<内网 IP>:8443/v1/chat/completions \
  -H "Authorization: Bearer sk-xc-1-..." -H "Content-Type: application/json" \
  -d '{"model":"openai/gpt-5","messages":[{"role":"user","content":"说一句话"}]}'
```

**接着做两件事：** 到上游厂商后台给这把 key 设一个信用上限（星槎的配额是事后判定，不是最后
一道钱刹车），并在 `/admin/quota` 设一条配额（结构化 Agent 最坏会调 `1+重试次数` 次模型）。

可观测是可选的：「设置」页配 OTLP endpoint，**默认关闭**——打开意味着提示词与模型输出会离开
这台机器，所以它必须是一次显式决定，不能是升级的副作用。

## 备份

```bash
DC="docker compose -f deploy/linux/docker-compose.yml --env-file .env"   # docker 那条路加这个前缀
$DC exec xingcha xingcha db backup    # VACUUM INTO，崩溃一致
$DC exec xingcha xingcha db verify    # 只读体检：完整性、schema 版本、行数、密文条数
$DC exec xingcha xingcha db restore /data/backups/xingcha-....db
```

不用 `cp`：WAL 下复制活库不是崩溃一致的，`-wal` 里可能还有未 checkpoint 的事务，拷出来的文件
可能根本打不开。

**密钥环单独备份，不要和数据库放同一个包**——那等于让加密对「备份泄露」这个最现实的威胁提供
零保护。密钥环丢失而数据库里已有密文时，星槎**拒绝启动**（静默重新生成会让上游 key 永久解不开，
而且当时不报错，等到下次真正调用上游才表现为一个莫名其妙的失败）。

```bash
tar czf xingcha-db-$(date +%F).tgz -C data backups   # 数据库（含 token 哈希与密文）
gpg -c data/secret.key                               # 密钥环，单独存
```

### 演练

```bash
./deploy/linux/drill.sh                # 备份 → 体检 → 挪走 data/ → 从备份重建 → 复原
./deploy/linux/drill.sh --no-keyring   # 验证"只恢复数据库、忘了密钥环"确实拒绝启动
./deploy/linux/drill.sh --keep         # 保留恢复出来的数据，不复原
```

**「`data/backups/` 里躺着一堆 .db 文件」这件事本身什么都不证明。** 备份不可信的三种方式都不会
在平时暴露：活库拷贝不是崩溃一致的、备份不含密钥环、备份文件本身可能是坏的。原目录是挪走而不是
删掉，演练失败时它就是退路。停机约一分钟，**每次改动部署方式之后跑一次**，以及至少每季度一次。

非 docker 那条路等价于：`db backup` → 把 `data/` 改名 → 重新起 → `db restore`。

## 安全注意

- **端口只绑 `127.0.0.1`，对外只经网关。** 绑到 `0.0.0.0` 既绕过 ufw 又绕过 TLS。
- **根证书要装到每台设备上**，见[网关](#网关)一节。
- **`data/` 不要放网络存储**（SQLite 的 WAL 会静默降级）。启动时会断言 WAL 并拒绝启动。
- **`.env` 里不要长期放上游 key。** 环境变量会进 `docker inspect` 与 `/proc/<pid>/environ`。
  星槎只在首次启动时加密导入数据库并告警，之后永久忽略。
- **后台密码要独立且足够长。** 它能改写上游 `base_url`——被打穿等于把付费 key 交出去。
- `data/` 权限 `700`，数据库与备份 `600`，容器以 UID 10001 非 root 运行。

## 排障

| 症状 | 先看 |
|---|---|
| 容器起不来 | `xc logs`。启动断言（WAL、密钥环、迁移）失败都会打印明确原因 |
| 重启循环 + `PermissionError: /data/backups` | `data/` 属主不对。`xc start` 会自动 chown（要 sudo） |
| 什么都打不开 | 网关没起。`./deploy/edge/edge status`，然后 `edge start` |
| 网关能开但回 502 | 星槎没起，或端口和 `Caddyfile` 里的 `reverse_proxy` 对不上。`xc status` |
| 浏览器一直拦证书 | 这台设备还没装根证书；Firefox 另需 `libnss3-tools` |
| 密码输对却一直跳回登录页 | cookie 带了 `Secure` 而你走的是 http。CI 有断言守这个 |
| `/v1` 返回 503 | 还没配上游 key。后台「上游」页（当场生效） |
| 磁盘水位 | `curl -sk https://<内网 IP>:8443/readyz`，低于 10% 标 `degraded` |
| 本机能开、别的机器连不上 | Windows 防火墙没放行；或 uv 直跑那条只绑了回环 |

`xingcha doctor` 一次性检查数据目录权限、schema 版本、密钥环、磁盘、代理环境变量与运行约束，
并对机器级 socks5 代理这类"报错看不出根因"的情况给出解释。
