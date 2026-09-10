# 部署

**网关只有一套：`deploy/edge/` 里的 Caddy 单文件**（一个可执行文件 + 一份 Caddyfile，
没有 docker）。它和星槎在同一台机器上，反代 `127.0.0.1:8720`，对外只开 8443。
星槎自己则按这台机器有没有 docker 分两条路：

| 这台机器 | 星槎怎么跑 | `.env` 里 |
|---|---|---|
| 有 docker | `./deploy/linux/xc start`（容器，端口只绑回环） | `XINGCHA_GATEWAY=edge` |
| 没有 docker | 双击 `deploy\windows\xc.bat`，或 `uv run xingcha serve` | 最后一节那两项 |

顺序是固定的——**先起网关，再起星槎**：

```bash
# 1. 网关：下载 + 起来 + 装根证书（一次性，三条命令都在 deploy/edge/CADDY.md）

# 2. 星槎：.env 里写 XINGCHA_GATEWAY=edge，然后
cd ~/Desktop/my-projects/xingcha && ./deploy/linux/xc start
```

首次会从 `deploy/.env.example` 生成一份 `.env` 并停下来。

**这台 Linux 没装 docker** 的话，第 1 步的网关一模一样，第 2 步换成 uv 直接在宿主上起：

```bash
# 1. 装 uv（一次性。装完**重开一个窗口**：PATH 是进程启动时读的）
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. 起服务
cd ~/Desktop/my-projects/xingcha && uv run --frozen --no-dev xingcha serve
```

就这两条，没有单独的 `uv sync`——`uv run` 会先按 `uv.lock` 把环境对齐再跑（`--frozen` 不许它就地改锁文件，
`--no-dev` 只装跑服务用得上的那些）。Python 也由 uv 自己装（`requires-python >=3.12`），系统里没有 3.12
不影响。前台跑，Ctrl-C 就是停止；data 在仓库根的 `data/`，和 docker 那条同一个位置。

**`.env` 也不用先复制**：没有它就全走默认值，`127.0.0.1:8720`、明文 HTTP、只有这台机器能打开。要改配置
再 `cp deploy/.env.example .env`（和 docker 那条**同一份**模板）——而要动的**不是**上面那张表里的
`XINGCHA_GATEWAY` / `XINGCHA_WEB_PORT`——那几项只有 compose 读，在这条路上写了也不生效——而是 `.env`
最后一节的 `XINGCHA_HOST` / `XINGCHA_PORT`，挂网关时再放开 `XINGCHA_TRUSTED_PROXIES=127.0.0.1`
与 `XINGCHA_PUBLIC_URL`。

这两条在 **Windows 上一样用**，只是装 uv 换成 `winget install --id astral-sh.uv`、`cd` 换成仓库路径；
不过那边直接双击 `deploy\windows\xc.bat` 更省事，它就是这两条外加生成 `.env`、打印一个能点开的地址。

两处差别容易踩：`./deploy/linux/xc` 与 `drill.sh` 整个是 docker 包装，这条路上没有对应物，日常动作直接敲
`uv run xingcha ...`；另外这里没有 `restart: unless-stopped` 那一层，**关掉终端就是停止**，要开机自启得
自己写一个 systemd 单元。

其余的（为什么不打镜像、HTTPS 怎么接、备份与演练）见下面「不走 docker」一节——网关是同一个，data 位置
与备份方式也一样。

---

## 为什么 TLS 要在同一台机器上终止

Caddy 与星槎在同一台机器上，两者之间那一跳走**宿主回环**，不出这台机器。把 Caddy
放到另一台去反代的话，浏览器那半段仍然是 HTTPS，而「Caddy → 星槎」那半段变成跨网络
的明文——最容易被误当成端到端加密的一种拓扑；顺带还让网关变成硬依赖：星槎那台好着、
网关那台挂了，服务就打不开。所以那条去掉了。

挂上网关时星槎那个端口**被强制只绑 `127.0.0.1`**（`xc` 的 `derive_bind_addr`，
不受 `XINGCHA_WEB_HOST` 影响），局域网上根本连不到，唯一进得来的就是本机那个 Caddy。
代价必须说清楚：

> **网关是硬依赖。** 它没起，后台就完全进不去——包括进去修东西。
> `./deploy/linux/xc start` 会在启动前检查 8443 上有没有人听，没有就给出可执行的
> 提示，而不是让你看到"容器 healthy 却什么都打不开"。

不挂网关时（`XINGCHA_GATEWAY` 留空）那个端口是明文 HTTP：后台密码与 `sk-xc-` 密钥
在网络上**裸传**；把 `XINGCHA_WEB_HOST` 填成 IP 还会让它绑到 `0.0.0.0`，而 docker
发布的端口走 `DOCKER-USER` 链、**绕过 ufw**——你在防火墙里写的 deny 对它无效。
所以"开给局域网"必须是一次显式选择。

反代后面还有一件必须做对的事：应用得**信任网关发来的 `X-Forwarded-Proto`**
（叠加层里的 `XINGCHA_TRUSTED_PROXIES=*`）。不信任的话应用以为自己在 http 上，
**会话 cookie 不带 `Secure`**——浏览器那半段明明是 HTTPS，却少了一层保护，
而功能完全正常，没人会注意到。敢用 `*` 的前提就是上面那条：端口只绑回环，
唯一能发这个头的就是本机那个 Caddy。

网关自己怎么起、根证书怎么装、Caddyfile 里每一行为什么少不了——**全在
[CADDY.md](edge/CADDY.md)**，这里不重复。

---

## 前置依赖

`docker` 与 `docker compose` v2（v1 的 `docker-compose` 已 EOL，不支持）。
缺什么 `xc` 会给出可以直接粘贴执行的安装命令。

---

## 日常动作

| 命令 | 做什么 |
|---|---|
| `./deploy/linux/xc start` | 重新构建代码并启动，**data 一个字节都不动** |
| `./deploy/linux/xc update` | 拉代码 + 重新构建启动（工作区脏时会拒绝，不会 `reset --hard`） |
| `./deploy/linux/xc redeploy` | 连数据一起清空，从零开始（会问一次 `yes`） |
| `./deploy/linux/xc stop` | 停止，data 保留 |
| `./deploy/linux/xc logs [n]` | 跟随日志 |
| `./deploy/linux/xc status` | 容器状态 + 后台账号状态 |

`xc` 存在的理由是那条正确的手敲命令太长，而**长命令里每一段都是踩过的坑**：

1. 容器还在跑的时候删 `data/`，进程握着已删除的 inode 继续写 —— 表现是"我删了库，
   密码却还在"；
2. 删掉之后 Docker 会用 **root** 重建挂载点，容器里 UID 10001 写不进去 ——
   直接进重启循环；
3. `COMPOSE_FILE` 那种"在哪个目录敲命令会改变结果"的配置 —— 在 `deploy/` 里
   `restart` 直接失败。

手敲的等价命令（路径相对于仓库根）：

```bash
docker compose -f deploy/linux/docker-compose.yml --env-file .env up -d --build
```

---

## `.env`

完整注释见 [`.env.example`](.env.example)。四项最常动的：

| 变量 | 默认 | 说明 |
|---|---|---|
| `XINGCHA_GATEWAY` | 空 | 空 = 自己发布端口、明文 HTTP；`edge` = 挂到本机那个 Caddy 上 |
| `XINGCHA_WEB_HOST` | `localhost` | 你在浏览器里敲的主机名或 IP |
| `XINGCHA_WEB_PORT` | `8720` | **独立跑时**发布的宿主端口；走网关时端口是网关的 8443 |
| `XINGCHA_ADMIN_PASSWORD` | 空 | 留空 = 没设置，首次访问 `/admin` 引导设定 |

`XINGCHA_WEB_HOST` 同时决定绑哪个接口（`localhost` → 只绑回环，其它 → `0.0.0.0`）。
合成一个变量是因为分成两个最容易出的错是二者对不上：页面上显示局域网 IP、实际只绑了
回环，于是别人打不开而那个地址看起来完全正确。

---

## 不走 docker（Windows，或没装 docker 的 Linux）

**Windows 双击 `deploy\windows\xc.bat`。** 它做四件事：确认有 `uv` → 没有 `.env` 就从
**同一份**模板生成 → `uv sync --frozen --no-dev` → `uv run xingcha serve`。窗口开着就是
跑着，关掉就是停止；data 在仓库根的 `data\` 下，和 docker 那条同一个位置。

没装 docker 的 Linux 是同样两条命令，只是没有那个批处理包装：`uv sync --frozen --no-dev`
然后 `uv run xingcha serve`。

这条路换掉的只是**怎么把进程跑起来**，配置来源、data 位置、备份方式都不变。

### 为什么这台不打镜像

不是图省事。Windows 上真上 docker 有一条硬伤：

> **data 不能放宿主目录。** Docker Desktop 经 9p/virtiofs 把 Windows 目录挂进
> 虚拟机，那是网络文件系统，**SQLite 的 WAL 在上面会静默降级**——症状是零星的
> `database is locked`，只在并发写时出现，压不出来也难复现。

绕开它只能改用命名卷，于是数据跑进了虚拟机里：备份、`db verify`、恢复演练全都得
进容器做。本地直跑没有这一条——`data\` 就是 NTFS 上的普通目录，WAL 是正常的，
备份就是几个能直接拷走的文件。

### HTTPS：网关是同一个

和 docker 那条**用的是同一套** `deploy/edge/`：下载一次可执行文件，起起来，
它反代的正是 `127.0.0.1:8720`。Windows 双击 `deploy\edge\edge.bat`，Linux
`./deploy/edge/edge start`。装根证书、日常命令、每一行配置为什么少不了——
全部见 [CADDY.md](edge/CADDY.md)。

差别只在星槎这边：这条路没有 `XINGCHA_GATEWAY` 那个开关（那是给 compose 看的），
所以 `.env` 最后一节的两项要手写（模板里有完整注释）：

| 变量 | 填什么 | 不填会怎样 |
|---|---|---|
| `XINGCHA_TRUSTED_PROXIES` | `127.0.0.1` | 会话 cookie 不带 `Secure`，而功能完全正常，没人会注意到 |
| `XINGCHA_PUBLIC_URL` | `https://<本机内网 IP>:8443` | 后台印出 `http://127.0.0.1:8720`，复制走的 curl 到别的机器上必然连不上 |

**`XINGCHA_HOST` 不用动。** Caddy 就在同一台机器上，走回环就够了；改成 `0.0.0.0`
等于在局域网上多开一条绕过 TLS 的明文入口，谁都能直连。

### 两个会卡住人的地方

- **Windows 防火墙。** 弹窗问要不要放行的是 `caddy.exe`（它要监听 8443）——**要允许，
  而且要勾「专用网络」**。点了取消的话本机 `https://127.0.0.1:8443` 完全正常，而别的
  机器一直连不上，那个现象指不到防火墙。
- **`uv` 装完要重开窗口。** PATH 是进程启动时读的，装 uv 的那个窗口里
  `where uv` 仍然找不到。

### 日常动作

`xc.bat` 只负责"起来"。其余的本来就不长、也没有 docker 那些坑，直接敲：

```
uv run xingcha doctor                  一次性体检
uv run xingcha admin status            后台账号状态
uv run xingcha admin reset-password    忘了密码
uv run xingcha db backup               崩溃一致的备份
uv run xingcha db verify               体检备份
```

更新就是 `git pull` 之后再双击一次——`uv sync --frozen` 会把依赖对齐。

> `deploy/linux/xc` 与 `deploy/linux/drill.sh` 是 docker 路径专用的，Windows 上不适用。
> 演练在这边等价于：`db backup` → 把 `data\` 改名 → 双击 → `db restore`。

---

## 初始化

打开 `https://<网关地址>:8443/admin`。

1. 首次访问引导**设置管理员密码**（至少 12 位，别复用其它服务的——这个后台能改写
   上游 `base_url`）。

   也可以在 `.env` 里预设 `XINGCHA_ADMIN_PASSWORD`，省掉这一步、也不怕忘。这一项
   **任意长度都生效**（太短会在启动日志里警告一次）。**先立者为准**：库里一旦有了
   密码，那一项就被忽略——这样任何能往 `.env` 写一行的人都顶不掉已建好的管理员密码。
   要改用它，先跑 `./deploy/linux/xc status` 确认状态，再
   `docker compose -f deploy/linux/docker-compose.yml --env-file .env exec xingcha xingcha admin reset-password`。

   忘了密码走同一条 `admin reset-password`，之后重新访问 `/admin` 设定。密码只存
   argon2id 哈希，没有别的找回途径。
2. 「上游」页填 key（或从这台机器上已有的厂商 key 变量里一键切换）。
3. 「密钥」页签发一把 `sk-xc-`，交给业务代码。

验证打通：

```bash
curl https://<网关地址>:8443/v1/chat/completions \
  -H "Authorization: Bearer sk-xc-1-..." \
  -H "Content-Type: application/json" \
  -d '{"model":"openai/gpt-5","messages":[{"role":"user","content":"说一句话"}]}'
```

业务代码只改两行：

```python
from openai import OpenAI
client = OpenAI(base_url="https://<网关地址>:8443/v1", api_key="sk-xc-1-...")
```

### ⚠️ 到上游厂商后台给这把 key 设信用上限

星槎的配额是**事后判定**（调用完才知道花了多少），所以它不是最后一道钱刹车。
上游侧的硬上限才是。

### 在 `/admin/quota` 设一条配额

结构化 Agent 最坏会调 `1+重试次数` 次模型，一条跑飞的调用能花掉几倍的钱。

---

## 开机自启与崩溃重启

`restart: unless-stopped` 已在编排里，不需要 systemd 单元。

---

## 可观测（可选）

「设置」页配 OTLP endpoint（Langfuse 之类）。**默认关闭**——打开意味着提示词与模型
输出会离开这台机器，而这个项目存在的理由恰恰是不想让请求经过别人手里，所以它必须是
一次显式的决定，不能是升级的副作用。

配好地址之后，每个 Agent 在自己的表单里选择要不要上报。

---

## 备份

```bash
DC="docker compose -f deploy/linux/docker-compose.yml --env-file .env"
$DC exec xingcha xingcha db backup
```

用 `VACUUM INTO` 做**崩溃一致**的副本（`cp` 复制活库在 WAL 下不是崩溃一致的，
`-wal` 里可能还有未 checkpoint 的事务，拷出来的文件可能根本打不开）。

**密钥环必须单独备份，而且不要和数据库放在同一个包里**：

```bash
# 数据库（含 token 哈希与 Fernet 密文）
tar czf xingcha-db-$(date +%F).tgz -C data backups

# 密钥环 —— 单独存，最好加一层口令
gpg -c data/secret.key
```

把密文和密钥打进同一个包，等于让加密对「备份泄露」这个最现实的威胁提供零保护。

恢复：

```bash
$DC exec xingcha xingcha db restore /data/backups/xingcha-....db
```

> 密钥环丢失而数据库里已有密文时，星槎会**拒绝启动**。这是有意的：静默重新生成
> 会让上游 key 永久解不开，而且当时不报任何错，等到下次真正调用上游时才表现为
> 一个莫名其妙的失败。

### 体检

```bash
$DC exec xingcha xingcha db verify
```

只读，随时可跑。报告完整性、schema 版本、各表行数，以及**里面有多少条要靠密钥环
才能解开的密文**——那一行是在提醒你：这个文件不是完整的备份。

### 演练

```bash
./deploy/linux/drill.sh
```

**「`data/backups/` 里躺着一堆 .db 文件」这件事本身什么都不证明。** 备份不可信的
三种方式都不会在平时暴露：

1. WAL 下 `cp` 活库不是崩溃一致的（所以星槎用 `VACUUM INTO`）；
2. 备份不含密钥环——只恢复数据库会得到一库永久解不开的密文；
3. 备份文件本身可能是坏的，而你只会在灾难当天发现。

`drill.sh` 会备份 → 体检 → **真的把 `data/` 整个挪走** → 从备份重建 → 等健康检查 →
复原。原目录是挪走而不是删掉，演练失败时它就是退路。演练期间**停机约一分钟**。

```bash
./deploy/linux/drill.sh --no-keyring   # 验证"只恢复数据库、忘了密钥环"确实会拒绝启动
./deploy/linux/drill.sh --keep         # 保留恢复出来的数据，不复原
```

**每次改动部署方式之后跑一次**，以及至少每季度一次。

---

## 安全注意

- **端口只绑 `127.0.0.1`，对外只经网关。** 别把它绑到 `0.0.0.0` —— 那既绕过 ufw
  （docker 发布的端口走 `DOCKER-USER` 链）又绕过 TLS，而且两条入口并存时人只会
  记住能打开的那一个。
- **网关的根证书要装到每台设备上。** 只有 `tls internal` 而继续点"继续前往"，
  只防被动嗅听：主动中间人递一张自签证书你同样会点过去。
- **`data/` 目录不要放网络存储。** SQLite 的 WAL 在上面会静默降级，症状是零星的
  `database is locked`。星槎启动时会断言 WAL 并拒绝启动，但把它放对地方更省事。
  （Docker Desktop 经 9p/virtiofs 挂进来的 Windows 目录也算——那正是 Windows 上
  不打镜像、直接本地跑的理由。）
- **`.env` 里不要长期放上游 key。** 环境变量会出现在 `docker inspect` 与
  `/proc/<pid>/environ`。星槎只在首次启动时把它加密导入数据库并告警，之后永久忽略。
- **后台密码要独立且足够长。** 它能改写上游 `base_url`——被打穿等于把付费 key 交出去。
- `data/` 权限是 `700`，数据库与备份是 `600`，容器以 UID 10001 非 root 运行。

---

## 排障

| 症状 | 先看 |
|---|---|
| 容器起不来 | `./deploy/linux/xc logs`。启动时的断言（WAL、密钥环、迁移）失败都会打印明确原因 |
| 重启循环 + `PermissionError: /data/backups` | `data/` 属主不对。`./deploy/linux/xc start` 会自动 chown（要 sudo） |
| 什么都打不开 | 网关没起。`./deploy/edge/edge status`，然后 `edge start` |
| 网关能开但回 502 | 星槎没起来，或没绑在 `127.0.0.1:8720` 上。`./deploy/linux/xc status` |
| 浏览器一直拦证书 | 这台设备还没装根证书，见 [CADDY.md](edge/CADDY.md) |
| 密码输对却一直跳回登录页 | cookie 带了 `Secure` 而你走的是 http。CI 有一条断言守这个，正常不该发生 |
| `/v1` 返回 503 | 还没配上游 key。后台「上游」页（当场生效） |
| 想看整体状况 | `./deploy/linux/xc status`，或 `xingcha doctor` |
| **Windows**：本机能开、别的机器连不上 | 防火墙没放行 `caddy.exe`（它监听 8443），或 Caddy 没在跑 |
| **Windows**：双击一闪就没 | 从 cmd 里跑一次 `deploy\windows\xc.bat` 看报错；失败路径都会 `pause`，正常不该一闪 |
| 磁盘水位 | `curl -sk https://<网关地址>:8443/readyz`，低于 10% 会标 `degraded` |

`xingcha doctor` 会一次性检查数据目录权限、schema 版本、密钥环、磁盘、代理环境变量
与运行约束，并对机器级 socks5 代理这类"报错看不出根因"的情况给出解释。
