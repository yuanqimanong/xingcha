# 部署

**一个容器、一个 compose 文件、明文 HTTP。**

```bash
git clone git@github.com:yuanqimanong/xingcha.git
cd xingcha && ./deploy/xc start
```

首次会从 `deploy/.env.example` 生成一份 `.env` 并停下来，让你决定要不要开给局域网。
填完再跑一次 `./deploy/xc start` 就起来了。Windows 用 `.\deploy\xc.ps1`，动作名一样。

---

## 为什么没有 TLS / 没有反向代理

早先这里是两个容器（xingcha + Caddy）、三份 compose、两份 Caddyfile，Caddy 负责
自动签证书。现在收敛成单容器明文 HTTP，这是一次**明确的取舍**，代价必须写清楚：

- **密码与 `sk-xc-` 密钥在网络上是裸传的。** 同网段的人 `tcpdump` 一开就能读到，
  ARP 欺骗都不用做。
- 之前那套用的是 Caddy 的内部 CA，浏览器不认，你每次都点"继续前往"。**那一档只防
  被动嗅听**：主动中间人递一张自己的自签证书，你同样会点过去。除非把 Caddy 的根 CA
  装进每台设备的信任库，否则它给的保护比看起来少。

所以：**自己的局域网可以这么用；放公网必须在前面加 TLS。** 加法是在前面放任何一个
反代（Caddy / nginx / Traefik），星槎的会话 cookie 会跟着请求协议自动带上 `Secure`
（见 `web/routes.py` 的 `cookie_secure`）；你需要额外给 uvicorn 开
`proxy_headers` 并把 `forwarded_allow_ips` 限定到反代的地址——**默认不开是有意的**，
开了就等于信任任何人伪造的 `X-Forwarded-Proto`。

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
| `XINGCHA_BIND_ADDR` | `127.0.0.1` | 宿主上绑哪个地址。**默认只有本机能访问**；开给局域网写 `0.0.0.0` |
| `XINGCHA_WEB_PORT` | `8720` | 宿主端口 |
| `XINGCHA_ADMIN_PASSWORD` | 空 | 留空 = 没设置，首次访问 `/admin` 引导设定 |

`XINGCHA_BIND_ADDR` 的默认值是一条**安全属性**：映射出去的端口走 Docker 的
`DOCKER-USER` 链，**会绕过 ufw** —— 你在防火墙里写的 deny 对它无效。所以"开给整个
局域网"必须是一次显式选择，而不是装上就默认对外。

**Windows 必须加一行** `XINGCHA_DATA_MOUNT=xingcha_data`：Docker Desktop 经
9p/virtiofs 把 Windows 目录挂进虚拟机，那是网络文件系统，**SQLite 的 WAL 在上面会
静默降级**——症状是零星的 `database is locked`，只在并发写时出现，压不出来也难复现。

---

## 初始化

打开 `http://<地址>:8720/admin`。

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
curl http://<地址>:8720/v1/chat/completions \
  -H "Authorization: Bearer sk-xc-1-..." \
  -H "Content-Type: application/json" \
  -d '{"model":"openai/gpt-5","messages":[{"role":"user","content":"说一句话"}]}'
```

业务代码只改两行：

```python
from openai import OpenAI
client = OpenAI(base_url="http://<地址>:8720/v1", api_key="sk-xc-1-...")
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

- **对外是明文 HTTP。** 见开头那节。放公网前面必须加 TLS。
- **默认只绑回环。** 改成 `0.0.0.0` 之前先想清楚：那个端口绕过 ufw。
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
| 别的设备访问不到 | `XINGCHA_BIND_ADDR` 还是默认的 `127.0.0.1` |
| 密码输对却一直跳回登录页 | cookie 带了 `Secure` 而你走的是 http。CI 有一条断言守这个，正常不该发生 |
| `/v1` 返回 503 | 还没配上游 key。后台「上游」页（当场生效） |
| 想看整体状况 | `./deploy/xc status`，或 `xingcha doctor` |
| 磁盘水位 | `curl -s http://<地址>:8720/readyz`，低于 10% 会标 `degraded` |

`xingcha doctor` 会一次性检查数据目录权限、schema 版本、密钥环、磁盘、代理环境变量
与运行约束，并对机器级 socks5 代理这类"报错看不出根因"的情况给出解释。
