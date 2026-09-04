# 部署（Linux · docker compose）

一键拉代码 → 构建镜像 → `docker compose up -d` → Caddy 自动 TLS。

> 只支持 docker compose 这一种形态。不同时提供 systemd 与裸 Dockerfile 两条路：
> 三种并列等于把三套运维范式一起变成事实上的接口，日后砍掉任一种都是毁约。

---

## 前置依赖

| 工具 | 安装 |
|------|------|
| **Git** | `apt-get update && apt-get install -y git` |
| **Docker** | `curl -fsSL https://get.docker.com \| sh` |
| **Compose v2** | `apt-get install -y docker-compose-plugin`（v1 的 `docker-compose` 已 EOL，不支持） |

一台 1C1G 的 VPS 足够。`deploy.sh` 会检查这些，缺什么就打印可直接执行的安装命令。

---

## 生产部署步骤（新加坡 VPS）

> **顺序有讲究**。第 2、3、4 步做反了会卡在最后一步，而且 Let's Encrypt 撞了速率
> 限制要等一周。

### 1. 确认网络可达性

星槎要在这台机器上直连 OpenRouter：

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://openrouter.ai/api/v1/models
```

返回 `200` 才继续。这台机器**不需要**代理——星槎的 HTTP 客户端一律
`trust_env=False`，不会继承机器级的 `ALL_PROXY`。如果机器上设了 socks5 代理而
星槎又继承了它，服务会在构造阶段直接 `ImportError` 且报错完全看不出跟代理有关，
所以这条路被显式堵死了。要走中转请在后台配 `openrouter.base_url`。

### 2. 先做 DNS

把域名的 A 记录指向这台机器的公网 IP，确认生效：

```bash
dig +short xc.example.com
```

**必须在起 Caddy 之前完成。** DNS 没生效时 Caddy 申请证书会失败，而失败次数会
计入 Let's Encrypt 的速率限制。

### 3. 再腾出 80/443

如果这台机器上还跑着 Dify（或别的占用 80/443 的东西）：

```bash
cd /path/to/dify/docker && docker compose down
```

先做 DNS 再停旧服务，是为了让"旧服务不可用"的窗口尽量短。

### 4. 克隆 + 首次部署

```bash
mkdir -p /opt && cd /opt
git clone -b master git@github.com:yuanqimanong/xingcha.git
cd xingcha/deploy
./deploy.sh
```

首次会从 `deploy/.env.example` 生成 `.env` 然后**主动停下**，提示你填写。

### 5. 填 `.env`

```env
XINGCHA_DOMAIN=xc.example.com
ACME_EMAIL=you@example.com

# 首次先用 staging 走一遍，确认链路后再注释掉
ACME_CA=https://acme-staging-v02.api.letsencrypt.org/directory
```

### 6. 用 staging 证书跑通一遍

```bash
./deploy.sh
curl -k https://xc.example.com/healthz     # -k 因为 staging 证书浏览器不认
```

拿到 `{"status":"ok"}` 说明 DNS、80 端口、防火墙、容器网络全都对了。

### 7. 切正式证书

注释掉 `.env` 里的 `ACME_CA`，然后：

```bash
docker compose up -d --force-recreate caddy
curl https://xc.example.com/healthz          # 这次不用 -k
```

### 8. 初始化

```
浏览器打开 https://xc.example.com/admin
```

1. 首次访问会引导**设置管理员密码**（至少 12 位，别复用其它服务的密码——
   这个后台能改写上游 base_url）
2. 「设置」页填 OpenRouter key
3. 「密钥」页签发一把 `sk-xc-`，交给业务代码

### 9. 验证打通

```bash
curl https://xc.example.com/v1/chat/completions \
  -H "Authorization: Bearer sk-xc-1-..." \
  -H "Content-Type: application/json" \
  -d '{"model":"openai/gpt-5","messages":[{"role":"user","content":"说一句话"}]}'
```

业务代码只需要改两行：

```python
from openai import OpenAI
client = OpenAI(base_url="https://xc.example.com/v1", api_key="sk-xc-1-...")
```

### 10. ⚠️ 到 OpenRouter 后台给这把上游 key 设信用上限

**即使星槎自己有配额闸，这一步也不能省。**

星槎的配额是进程内计数的：进程没起来、迁移失败、或者你哪天把规则删了，闸就不在了。
OpenRouter 侧的额度上限是唯一不依赖星槎正常工作的那道闸。

### 11. 在 `/admin/quota` 设一条配额

三级主体（用户 / 令牌 / Agent）× 三窗口（日 / 月 / 累计），金额与次数上限可分别设。

两点要知道：

- **金额扣的是上游报的实际费用**，拿不到才回落目录估价。运行列表里带 `~` 前缀的
  是估价，不带的是实价。
- **直通路径默认不受配额约束**（契约 §3.9 冻结了这一点）。要打开，设
  `XINGCHA_QUOTA_ON_PASSTHROUGH=1` 并重启——注意这对调用方是一次**收紧**，
  原本能跑的请求会开始收到 429。

---

## 更新

```bash
cd /opt/xingcha/deploy
./deploy.sh
```

拉最新代码 → 重新构建 → `up -d`。数据库迁移在容器启动时自动跑，**迁移前会自动
备份**（`VACUUM INTO` 到 `data/backups/`）。

升级期间有 1–2 秒中断，正在跑的长请求会被切断——这是已选定的升级档位接受的代价。
`stop_grace_period` 是 30 秒的折中：设成和 `request_timeout`（600s）一样长的话，
每次升级要等 10 分钟。

只更新不启动：

```bash
./deploy.sh --no-run
```

> 更新对已存在的仓库执行 `git reset --hard origin/master`：**以远程为准、丢弃部署机
> 上的本地代码改动**（`.env` 与 `data/` 是 untracked，不受影响）。部署机上不要手改代码。

---

## 开机自启与崩溃重启

已经有了：`restart: unless-stopped`。开机时 Docker 守护进程会把它拉起来，
崩溃时也会。确认 Docker 自己开机自启：

```bash
systemctl enable docker
```

---

## 备份

```bash
docker compose exec xingcha xingcha db backup
```

用 `VACUUM INTO` 做**崩溃一致**的副本（`cp` 复制活库在 WAL 下不是崩溃一致的，
`-wal` 里可能还有未 checkpoint 的事务，拷出来的文件可能根本打不开）。

**密钥环必须单独备份，而且不要和数据库放在同一个包里**：

```bash
# 数据库（含 token 哈希与 Fernet 密文）
tar czf xingcha-db-$(date +%F).tgz -C /opt/xingcha/data backups

# 密钥环 —— 单独存，最好加一层口令
gpg -c /opt/xingcha/data/secret.key
```

把密文和密钥打进同一个包，等于让加密对「备份泄露」这个最现实的威胁提供零保护。

恢复：

```bash
docker compose exec xingcha xingcha db restore /data/backups/xingcha-....db
```

> 密钥环丢失而数据库里已有密文时，星槎会**拒绝启动**。这是有意的：静默重新生成
> 会让上游 key 永久解不开，而且当时不报任何错，等到下次真正调用上游时才表现为
> 一个莫名其妙的失败。

---

## 脚本参数

| 参数 | 说明 |
|---|---|
| `--no-run` | 只拉代码 + 构建，不启动 |
| `--repo-dir <目录>` | 仓库目录（全新机器指定克隆目标；默认取脚本上级 = 仓库根） |
| `--repo-url <地址>` | 仓库地址（仅首次克隆用） |
| `--branch <分支>` | 默认 `master` |

---

## 安全注意

- **不要给 xingcha 容器加 `ports:`。** 加一行 `ports: 8720:8720` 会让应用直接暴露
  在公网上，而且 **Docker 的 `DOCKER-USER` 链会绕过 ufw**：防火墙规则写了 deny，
  映射出去的端口照样可达。对外只经 Caddy。
- **`data/` 目录不要放网络存储。** SQLite 的 WAL 在上面会静默降级，症状是零星的
  `database is locked`。星槎启动时会断言 WAL 并拒绝启动，但把它放对地方更省事。
- **`.env` 里不要长期放上游 key。** 环境变量会出现在 `docker inspect` 与
  `/proc/<pid>/environ`。星槎只在首次启动时把它加密导入数据库并告警，之后永久忽略。
- **后台密码要独立且足够长。** 它能改写上游 base_url——被打穿等于把付费 key 交出去。
- `data/` 权限是 `700`，数据库与备份是 `600`，容器以 UID 10001 非 root 运行。

---

## 排障

| 症状 | 先看 |
|---|---|
| 容器起不来 | `docker compose logs xingcha`。启动时的断言（WAL、密钥环、迁移）失败都会打印明确原因 |
| 证书签不下来 | `docker compose logs caddy`。多半是 DNS 没生效或 80 端口被占 |
| `/v1` 返回 503 | 还没配 OpenRouter key。后台「设置」页，或 `docker compose exec xingcha xingcha config set openrouter.api_key -` |
| 想看整体状况 | `docker compose exec xingcha xingcha doctor` |
| 磁盘水位 | `curl -s https://<域名>/readyz`，低于 10% 会标 `degraded` |

`xingcha doctor` 会一次性检查数据目录权限、schema 版本、密钥环、磁盘、代理环境变量
与运行约束，并对机器级 socks5 代理这类"报错看不出根因"的情况给出解释。
