# 部署

**一个容器，对外只有一条路：共享网关上的 HTTPS。**

顺序是固定的——**先起网关，再部署 xingcha**：

```bash
# 1. 网关（独立项目，fin / pyp 共用同一台）。文档：../edge/README.md
cd ~/Desktop/my-project/edge && ./edge start

# 2. 在每台要访问的设备上装一次根证书（上一步会打印命令）
#    不装的话浏览器每次都拦，而点"继续前往"只防被动嗅听

# 3. xingcha
cd ~/Desktop/my-project/xingcha && ./deploy/xc start
```

首次会从 `deploy/.env.example` 生成一份 `.env` 并停下来。Windows 用
`.\deploy\xc.ps1`，动作名一样。

---

## 为什么只有一条路

曾经有两条：直连明文 HTTP（宿主端口 8720）与经网关的 HTTPS（8443）。去掉直连不是
为了少一个选项，而是因为**两条路的安全性质不同，而人只会记住能打开的那一个**：

- 直连那条上，后台密码与 `sk-xc-` 密钥在网络上**裸传**；
- 而那个宿主端口走 Docker 的 `DOCKER-USER` 链，**绕过 ufw**——你在防火墙里写的
  deny 对它无效。

现在 xingcha **一个宿主端口都不发布**（compose 里只有 `expose`），唯一入口是
`edge` 网络里的网关。代价必须说清楚：

> **网关是硬依赖。** 它没起，后台就完全进不去——包括进去修东西。
> `./deploy/xc start` 会在启动前检查它，缺了就给出可执行的提示，
> 而不是让你看到"容器 healthy 却什么都打不开"。

反代后面还有一件必须做对的事：应用得**信任网关发来的 `X-Forwarded-Proto`**
（compose 里的 `XINGCHA_TRUSTED_PROXIES=*`）。不信任的话应用以为自己在 http 上，
**会话 cookie 不带 `Secure`**——浏览器那半段明明是 HTTPS，却少了一层保护，
而功能完全正常，没人会注意到。敢用 `*` 的前提就是上面那条：零宿主端口。

---

## 前置依赖

`docker` 与 `docker compose` v2（v1 的 `docker-compose` 已 EOL，不支持）。
缺什么 `xc` 会给出可以直接粘贴执行的安装命令。

---

## 日常动作

| 命令 | 做什么 |
|---|---|
| `./deploy/xc start` | 重新构建代码并启动，**data 一个字节都不动** |
| `./deploy/xc update` | 拉代码 + 重新构建启动（工作区脏时会拒绝，不会 `reset --hard`） |
| `./deploy/xc redeploy` | 连数据一起清空，从零开始（会问一次 `yes`） |
| `./deploy/xc stop` | 停止，data 保留 |
| `./deploy/xc logs [n]` | 跟随日志 |
| `./deploy/xc status` | 容器状态 + 后台账号状态 |

`xc` 存在的理由是那条正确的手敲命令太长，而**长命令里每一段都是踩过的坑**：

1. 容器还在跑的时候删 `data/`，进程握着已删除的 inode 继续写 —— 表现是"我删了库，
   密码却还在"；
2. 删掉之后 Docker 会用 **root** 重建挂载点，容器里 UID 10001 写不进去 ——
   直接进重启循环；
3. `COMPOSE_FILE` 那种"在哪个目录敲命令会改变结果"的配置 —— 在 `deploy/` 里
   `restart` 直接失败。

手敲的等价命令（路径相对于仓库根）：

```bash
docker compose -f deploy/docker-compose.yml --env-file .env up -d --build
```

---

## `.env`

完整注释见 [`.env.example`](.env.example)。三项最常动的：

| 变量 | 默认 | 说明 |
|---|---|---|
| `XINGCHA_WEB_HOST` | `localhost` | 你在浏览器里敲的主机名或 IP（**网关**的） |
| `XINGCHA_WEB_PORT` | `8443` | 网关上分给 xingcha 的端口（`edge/.env` 的 `PORT_XINGCHA`） |
| `XINGCHA_ADMIN_PASSWORD` | 空 | 留空 = 没设置，首次访问 `/admin` 引导设定 |

前两项**只用于拼后台里展示的 curl 示例**，不影响监听——容器根本不监听宿主端口。
填错的后果是用户复制那条 curl 命令连不上，而错误信息指不到"该走网关"。

**Windows 必须加一行** `XINGCHA_DATA_MOUNT=xingcha_data`：Docker Desktop 经
9p/virtiofs 把 Windows 目录挂进虚拟机，那是网络文件系统，**SQLite 的 WAL 在上面会
静默降级**——症状是零星的 `database is locked`，只在并发写时出现，压不出来也难复现。

---

## 初始化

打开 `https://<网关地址>:8443/admin`。

1. 首次访问引导**设置管理员密码**（至少 12 位，别复用其它服务的——这个后台能改写
   上游 `base_url`）。

   也可以在 `.env` 里预设 `XINGCHA_ADMIN_PASSWORD`，省掉这一步、也不怕忘。这一项
   **任意长度都生效**（太短会在启动日志里警告一次）。**先立者为准**：库里一旦有了
   密码，那一项就被忽略——这样任何能往 `.env` 写一行的人都顶不掉已建好的管理员密码。
   要改用它，先跑 `./deploy/xc status` 确认状态，再
   `docker compose -f deploy/docker-compose.yml --env-file .env exec xingcha xingcha admin reset-password`。

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
DC="docker compose -f deploy/docker-compose.yml --env-file .env"
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
./deploy/drill.sh
```

**「`data/backups/` 里躺着一堆 .db 文件」这件事本身什么都不证明。** 备份不可信的
三种方式都不会在平时暴露：

1. WAL 下 `cp` 活库不是崩溃一致的（所以星槎用 `VACUUM INTO`）；
2. 备份不含密钥环——只恢复数据库会得到一库永久解不开的密文；
3. 备份文件本身可能是坏的，而你只会在灾难当天发现。

`drill.sh` 会备份 → 体检 → **真的把 `data/` 整个挪走** → 从备份重建 → 等健康检查 →
复原。原目录是挪走而不是删掉，演练失败时它就是退路。演练期间**停机约一分钟**。

```bash
./deploy/drill.sh --no-keyring   # 验证"只恢复数据库、忘了密钥环"确实会拒绝启动
./deploy/drill.sh --keep         # 保留恢复出来的数据，不复原
```

**每次改动部署方式之后跑一次**，以及至少每季度一次。

---

## 安全注意

- **零宿主端口，对外只经网关。** 别给这个服务加 `ports:` —— 那既绕过 ufw 又绕过 TLS。
- **网关的根证书要装到每台设备上。** 只有 `tls internal` 而继续点"继续前往"，
  只防被动嗅听：主动中间人递一张自签证书你同样会点过去。
- **`data/` 目录不要放网络存储。** SQLite 的 WAL 在上面会静默降级，症状是零星的
  `database is locked`。星槎启动时会断言 WAL 并拒绝启动，但把它放对地方更省事。
  （Windows 上的宿主目录就属于这一类，所以要用命名卷。）
- **`.env` 里不要长期放上游 key。** 环境变量会出现在 `docker inspect` 与
  `/proc/<pid>/environ`。星槎只在首次启动时把它加密导入数据库并告警，之后永久忽略。
- **后台密码要独立且足够长。** 它能改写上游 `base_url`——被打穿等于把付费 key 交出去。
- `data/` 权限是 `700`，数据库与备份是 `600`，容器以 UID 10001 非 root 运行。

---

## 排障

| 症状 | 先看 |
|---|---|
| 容器起不来 | `./deploy/xc logs`。启动时的断言（WAL、密钥环、迁移）失败都会打印明确原因 |
| 重启循环 + `PermissionError: /data/backups` | `data/` 属主不对。`./deploy/xc start` 会自动 chown（要 sudo） |
| 什么都打不开 | 网关没起。`cd ../edge && ./edge start` |
| 网关能开但回 502 | xingcha 不在 `edge` 网络里，或没起来。`./deploy/xc status` |
| 浏览器一直拦证书 | 这台设备还没装根证书。`cd ../edge && ./edge ca` |
| 密码输对却一直跳回登录页 | cookie 带了 `Secure` 而你走的是 http。CI 有一条断言守这个，正常不该发生 |
| `/v1` 返回 503 | 还没配上游 key。后台「上游」页（当场生效） |
| 想看整体状况 | `./deploy/xc status`，或 `xingcha doctor` |
| 磁盘水位 | `curl -sk https://<网关地址>:8443/readyz`，低于 10% 会标 `degraded` |

`xingcha doctor` 会一次性检查数据目录权限、schema 版本、密钥环、磁盘、代理环境变量
与运行约束，并对机器级 socks5 代理这类"报错看不出根因"的情况给出解释。
