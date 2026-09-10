# 部署

网关是 `deploy/edge/` 里的 Caddy 单文件（一个可执行文件 + 一份 Caddyfile，没有 docker）。
它和星槎在同一台机器上，反代 `127.0.0.1:8720`，对外只开 8443。星槎自己按这台机器有没有
docker 分两条路：

| 这台机器 | 星槎怎么跑 | `.env` 里 |
|---|---|---|
| 有 docker | `./deploy/linux/xc start`（容器，端口只绑回环） | `XINGCHA_GATEWAY=edge` |
| 没有 docker | 双击 `deploy\windows\xc.bat`，或 `uv run xingcha serve` | 见「不走 docker」一节 |

**顺序固定：先起网关，再起星槎。** 网关怎么起、根证书怎么装、Caddyfile 每一行为什么
少不了，全在 [CADDY.md](edge/CADDY.md)。

前置依赖：`docker` 与 `docker compose` v2（v1 的 `docker-compose` 已 EOL）。缺什么
`xc` 会给出可直接粘贴的安装命令。

```bash
# .env 里写 XINGCHA_GATEWAY=edge，然后
./deploy/linux/xc start     # 首次会从 deploy/.env.example 生成 .env 并停下来提示填写
```

---

## 日常动作

| 命令 | 做什么 |
|---|---|
| `./deploy/linux/xc start` | 重新构建代码并启动，**data 一个字节都不动** |
| `./deploy/linux/xc update` | 拉代码 + 重新构建启动（工作区脏时拒绝，不会 `reset --hard`） |
| `./deploy/linux/xc redeploy` | 连数据一起清空（会问一次 `yes`） |
| `./deploy/linux/xc stop` | 停止，data 保留 |
| `./deploy/linux/xc logs [n]` | 跟随日志 |
| `./deploy/linux/xc status` | 容器状态 + 后台账号状态 |

一律走 `xc`，不要手敲 compose：它替你把 `-f` 与 `--env-file` 传好、启动前检查网关在不在，
并挡住三个踩过的坑——容器还在跑时删 `data/`（进程握着已删除的 inode 继续写）、
删完 Docker 用 root 重建挂载点（容器里 UID 10001 写不进去，直接重启循环）、
`COMPOSE_FILE` 那种"在哪个目录敲命令会改变结果"的配置。

`restart: unless-stopped` 已在编排里，开机自启不需要 systemd 单元。

---

## `.env`

完整注释见 [`.env.example`](.env.example)。四项最常动的：

| 变量 | 默认 | 说明 |
|---|---|---|
| `XINGCHA_GATEWAY` | 空 | 空 = 自己发布端口、明文 HTTP；`edge` = 挂到本机那个 Caddy 上 |
| `XINGCHA_WEB_HOST` | `localhost` | 你在浏览器里敲的主机名或 IP |
| `XINGCHA_WEB_PORT` | `8720` | **独立跑时**发布的宿主端口；走网关时端口是网关的 8443 |
| `XINGCHA_ADMIN_PASSWORD` | 空 | 留空 = 首次访问 `/admin` 引导设定 |

`XINGCHA_WEB_HOST` 同时决定绑哪个接口（`localhost` → 只绑回环，其它 → `0.0.0.0`）。
合成一个变量是因为分成两个最容易出的错是二者对不上：页面上显示局域网 IP、实际只绑了
回环，于是别人打不开而那个地址看起来完全正确。

> **`XINGCHA_WEB_PORT` 改了就要改 Caddyfile。** 网关的 `reverse_proxy` 写的是
> `127.0.0.1:8720`，两边不一致时网关回 502，而星槎自己完全健康。

---

## 拓扑与它的代价

挂网关时星槎那个端口**被强制只绑 `127.0.0.1`**（`xc` 的 `derive_bind_addr`，不受
`XINGCHA_WEB_HOST` 影响），唯一进得来的就是本机那个 Caddy。

> **网关因此是硬依赖。** 它没起，后台完全进不去——包括进去修东西。`xc start` 会先检查
> 8443 上有没有人听。

TLS 必须在同一台机器上终止：Caddy 到星槎那一跳走宿主回环，不出这台机器。把 Caddy 放到
另一台去反代，浏览器那半段仍是 HTTPS，而「Caddy → 星槎」那半段变成跨网络明文——最容易
被误当成端到端加密的一种拓扑。

反代后面还有一件必须做对：应用得**信任网关发来的 `X-Forwarded-Proto`**（叠加层里的
`XINGCHA_TRUSTED_PROXIES=*`）。不信任的话应用以为自己在 http 上，**会话 cookie 不带
`Secure`**，而功能完全正常，没人会注意到。

不挂网关时那个端口是明文 HTTP：后台密码与 `sk-xc-` 密钥**裸传**；把 `XINGCHA_WEB_HOST`
填成 IP 还会让它绑到 `0.0.0.0`，而 docker 发布的端口走 `DOCKER-USER` 链、**绕过 ufw**。
所以"开给局域网"必须是一次显式选择。

---

## 初始化

打开 `https://<网关地址>:8443/admin`。

1. 引导设置管理员密码（至少 12 位，别复用其它服务的——这个后台能改写上游 `base_url`）。
   也可以在 `.env` 里预设 `XINGCHA_ADMIN_PASSWORD`。**先立者为准**：库里一旦有了密码，
   那一项就被忽略，这样任何能往 `.env` 写一行的人都顶不掉已建好的密码。忘了密码走
   `xingcha admin reset-password`，之后重新访问 `/admin` 设定。
2. 「上游」页填 key。
3. 「密钥」页签发一把 `sk-xc-`，交给业务代码。

```bash
curl https://<网关地址>:8443/v1/chat/completions \
  -H "Authorization: Bearer sk-xc-1-..." -H "Content-Type: application/json" \
  -d '{"model":"openai/gpt-5","messages":[{"role":"user","content":"说一句话"}]}'
```

**接着做两件事：** 到上游厂商后台给这把 key 设一个信用上限（星槎的配额是事后判定，
不是最后一道钱刹车），并在 `/admin/quota` 设一条配额（结构化 Agent 最坏会调
`1+重试次数` 次模型）。

可观测是可选的：「设置」页配 OTLP endpoint，**默认关闭**——打开意味着提示词与模型输出会
离开这台机器，所以它必须是一次显式决定，不能是升级的副作用。配好地址后每个 Agent 各自
选择要不要上报。

---

## 备份

```bash
DC="docker compose -f deploy/linux/docker-compose.yml --env-file .env"
$DC exec xingcha xingcha db backup    # VACUUM INTO，崩溃一致
$DC exec xingcha xingcha db verify    # 只读体检：完整性、schema 版本、行数、密文条数
$DC exec xingcha xingcha db restore /data/backups/xingcha-....db
```

不用 `cp`：WAL 下复制活库不是崩溃一致的，`-wal` 里可能还有未 checkpoint 的事务，
拷出来的文件可能根本打不开。

**密钥环单独备份，不要和数据库放同一个包**——那等于让加密对「备份泄露」这个最现实的
威胁提供零保护：

```bash
tar czf xingcha-db-$(date +%F).tgz -C data backups   # 数据库（含 token 哈希与密文）
gpg -c data/secret.key                               # 密钥环，单独存
```

> 密钥环丢失而数据库里已有密文时，星槎**拒绝启动**。静默重新生成会让上游 key 永久
> 解不开，而且当时不报任何错，等到下次真正调用上游时才表现为一个莫名其妙的失败。

### 演练

```bash
./deploy/linux/drill.sh                # 备份 → 体检 → 挪走 data/ → 从备份重建 → 复原
./deploy/linux/drill.sh --no-keyring   # 验证"只恢复数据库、忘了密钥环"确实拒绝启动
./deploy/linux/drill.sh --keep         # 保留恢复出来的数据，不复原
```

**「`data/backups/` 里躺着一堆 .db 文件」这件事本身什么都不证明。** 备份不可信的三种
方式都不会在平时暴露：活库拷贝不是崩溃一致的、备份不含密钥环、备份文件本身可能是坏的。
原目录是挪走而不是删掉，演练失败时它就是退路。演练期间停机约一分钟。

**每次改动部署方式之后跑一次**，以及至少每季度一次。

---

## 不走 docker（Windows，或没装 docker 的 Linux）

**Windows 双击 `deploy\windows\xc.bat`**：确认有 `uv` → 没有 `.env` 就从**同一份**模板
生成 → `uv sync --frozen --no-dev` → `uv run xingcha serve`。窗口开着就是跑着，关掉就是
停止；data 在仓库根的 `data\`，和 docker 那条同一个位置。

Linux 上是两条命令：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # 一次性。装完**重开一个窗口**
uv run --frozen --no-dev xingcha serve
```

没有单独的 `uv sync`——`uv run` 会先按 `uv.lock` 对齐环境再跑。Python 也由 uv 自己装。
`.env` 不用先复制：没有它就全走默认值（`127.0.0.1:8720`、明文 HTTP、只有本机能开）。

**Windows 上不打镜像不是图省事**：Docker Desktop 经 9p/virtiofs 把 Windows 目录挂进
虚拟机，那是网络文件系统，**SQLite 的 WAL 在上面会静默降级**——症状是零星的
`database is locked`，只在并发写时出现，压不出来也难复现。绕开只能改用命名卷，于是备份、
`db verify`、恢复演练全都得进容器做。

网关是**同一套** `deploy/edge/`（Windows 双击 `deploy\edge\edge.bat`）。差别只在星槎这边：
这条路没有 `XINGCHA_GATEWAY` 那个开关（那是给 compose 看的），所以 `.env` 最后一节的两项
要手写：

| 变量 | 填什么 | 不填会怎样 |
|---|---|---|
| `XINGCHA_TRUSTED_PROXIES` | `127.0.0.1` | 会话 cookie 不带 `Secure`，而功能完全正常 |
| `XINGCHA_PUBLIC_URL` | `https://<本机内网 IP>:8443` | 后台印出 `http://127.0.0.1:8720`，复制走的 curl 到别的机器上必然连不上 |

**`XINGCHA_HOST` 不用动**——Caddy 就在同一台机器上，改成 `0.0.0.0` 等于在局域网上多开
一条绕过 TLS 的明文入口。

两个会卡住人的地方：**Windows 防火墙**弹窗问的是 `caddy.exe`（它监听 8443），要允许
且要勾「专用网络」，点取消的话本机正常而别的机器一直连不上；**`uv` 装完要重开窗口**，
PATH 是进程启动时读的。

日常动作直接敲（`deploy/linux/xc` 与 `drill.sh` 整个是 docker 包装，这条路上不适用）：

```
uv run xingcha doctor / admin status / admin reset-password / db backup / db verify
```

更新就是 `git pull` 之后再双击一次。这条路没有 `restart: unless-stopped`，**关掉终端
就是停止**，开机自启要自己写一个 systemd 单元。演练在这边等价于：`db backup` → 把
`data\` 改名 → 双击 → `db restore`。

---

## 安全注意

- **端口只绑 `127.0.0.1`，对外只经网关。** 绑到 `0.0.0.0` 既绕过 ufw 又绕过 TLS，
  而且两条入口并存时人只会记住能打开的那一个。
- **网关的根证书要装到每台设备上**，见 [CADDY.md](edge/CADDY.md)。继续点"继续前往"
  只防被动嗅听：主动中间人递一张自签证书你同样会点过去。
- **`data/` 不要放网络存储**（SQLite 的 WAL 会静默降级）。启动时会断言 WAL 并拒绝启动，
  但放对地方更省事。
- **`.env` 里不要长期放上游 key。** 环境变量会进 `docker inspect` 与
  `/proc/<pid>/environ`。星槎只在首次启动时加密导入数据库并告警，之后永久忽略。
- **后台密码要独立且足够长。** 它能改写上游 `base_url`——被打穿等于把付费 key 交出去。
- `data/` 权限 `700`，数据库与备份 `600`，容器以 UID 10001 非 root 运行。

---

## 排障

| 症状 | 先看 |
|---|---|
| 容器起不来 | `xc logs`。启动断言（WAL、密钥环、迁移）失败都会打印明确原因 |
| 重启循环 + `PermissionError: /data/backups` | `data/` 属主不对。`xc start` 会自动 chown（要 sudo） |
| 什么都打不开 | 网关没起。`./deploy/edge/edge status`，然后 `edge start` |
| 网关能开但回 502 | 星槎没起，或端口和 Caddyfile 里的 `reverse_proxy` 对不上。`xc status` |
| 浏览器一直拦证书 | 这台设备还没装根证书，见 [CADDY.md](edge/CADDY.md) |
| 密码输对却一直跳回登录页 | cookie 带了 `Secure` 而你走的是 http。CI 有断言守这个 |
| `/v1` 返回 503 | 还没配上游 key。后台「上游」页（当场生效） |
| 磁盘水位 | `curl -sk https://<网关地址>:8443/readyz`，低于 10% 标 `degraded` |
| **Windows**：本机能开、别的机器连不上 | 防火墙没放行 `caddy.exe` |
| **Windows**：双击一闪就没 | 从 cmd 里跑一次 `deploy\windows\xc.bat` 看报错 |

`xingcha doctor` 一次性检查数据目录权限、schema 版本、密钥环、磁盘、代理环境变量与
运行约束，并对机器级 socks5 代理这类"报错看不出根因"的情况给出解释。
