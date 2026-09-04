# 升级

星槎的升级档位是 **契约冻结 + 数据自动迁移**：调用方手里的 `base_url`、
`sk-xc-` 密钥与 `model` 字符串永不改变，升级期间接受 1–2 秒中断。

```bash
cd /opt/xingcha/deploy && ./deploy.sh
```

就这一条。下面是它背后发生的事，以及出问题时该看哪里。

---

## 升级时发生了什么

```
git reset --hard origin/master     以远程为准，丢弃部署机上的代码改动
docker compose build               重新构建镜像
docker compose up -d               停旧容器（最多等 30 秒）→ 起新容器
  └─ 容器启动时：
       1  umask + 数据目录权限
       2  VACUUM INTO 备份到 data/backups/     ← 只在真要迁移时才做
       3  alembic upgrade head
       4  密钥环（缺环而库里有密文 → 拒绝启动）
       5  PRAGMA journal_mode 断言 → 不是 wal 就拒绝启动
       6  预热模型目录
```

**每一条断言失败都是拒绝启动，而不是警告。** 带着半旧的 schema 或错误的
journal 模式继续服务，症状会在几小时后以完全看不出根因的形式出现。

---

## 为什么升级不会打断调用方

三件事一起保证：

**1. 对外契约是冻结的。** 路径归属、令牌格式、`model` 命名空间、响应形状、错误码、
SSE 帧序列全部写在 [CONTRACT.md](CONTRACT.md) 里，由 `contract.py` 的常量生成，
并有一套黄金测试锁着。改动任一闭集都会让 CI 变红。

**2. 数据库迁移是 expand-contract 的。** 一次升级**只允许加**（加列、加表、加索引）。
删列、改列、改语义必须拆成两个版本发布——因为新容器跑迁移时，旧容器可能还在服务。

**3. 迁移前自动备份。** `VACUUM INTO`（不是 `cp`：WAL 下直接复制活库不是崩溃一致的，
`-wal` 里可能还有未 checkpoint 的事务，拷出来的文件可能根本打不开）。

---

## 中断的确切范围

| | |
|---|---|
| 中断时长 | 1–2 秒（`docker compose up -d` 换容器的时间） |
| 已完成的请求 | 不受影响 |
| **正在跑的长请求** | **会被切断**（`stop_grace_period: 30s`） |
| 已签发的密钥 | 不受影响，永不失效 |
| 内存里的用量记录 | 关停时强制落盘，不丢 |

长请求被切断是已选定档位接受的代价。`stop_grace_period` 设成 30 秒是折中：
设成和 `request_timeout`（600 秒）一样长的话，每次升级要等 10 分钟。

> 设计上没有堵死零中断：应用除 SQLite 外无状态，日后改 Caddyfile 成两上游 +
> health check 即可做蓝绿。前提是迁移向后兼容，而那已经由 expand-contract 纪律保证。

---

## 回滚

先确认要回滚的是**代码**还是**数据**。

### 只回代码（schema 没变）

```bash
cd /opt/xingcha
git reset --hard <上一个 commit>
cd deploy && ./deploy.sh
```

新版本没有加迁移时，旧代码跑在新库上是安全的——库里只是多了几列没人读。

### 代码 + schema 都要回

```bash
docker compose exec xingcha xingcha db downgrade <目标 revision> --yes
cd /opt/xingcha && git reset --hard <上一个 commit>
cd deploy && ./deploy.sh
```

`downgrade` **总是先备份**。每个迁移都必须有能跑通的 `downgrade()`，
`tests/test_db_schema.py` 里有 `upgrade → downgrade → upgrade` 的演练。

### 数据本身出问题

```bash
docker compose exec xingcha ls /data/backups
docker compose exec xingcha xingcha db restore /data/backups/xingcha-<时间戳>.db --yes
docker compose restart xingcha
```

恢复前会先跑一次 `PRAGMA integrity_check`——不能用一个坏文件覆盖好库。

---

## 升级前值得跑一遍的演练

在真实数据的副本上验证迁移，而不是在空库上：

```bash
# 1 取一份线上库的崩溃一致副本
docker compose exec xingcha xingcha db backup --tag pre-upgrade-drill

# 2 在副本上跑新版本的迁移
cp data/backups/xingcha-<时间戳>-pre-upgrade-drill.db /tmp/drill.db
XINGCHA_DATA_DIR=/tmp/drill-dir xingcha db upgrade

# 3 逐字段确认既有数据没变
sqlite3 /tmp/drill.db "SELECT COUNT(*), SUM(input_tokens) FROM run_usage;"
```

空库上的 `upgrade` 通过，证明不了"有真实数据时也无感"。

---

## 密钥环

`data/secret.key` 是 MultiFernet 密钥环（每行一把，首行为当前加密用的）。

**它丢了，setting 表里的上游 key 就永久解不开。** 星槎在这种情况下**拒绝启动**
而不是静默重新生成——静默重生成当时不报任何错，等到下次真正调用上游时才表现为
一个莫名其妙的失败，那是一扇单向门。

轮换密钥是纯加法（在文件头部插一行新 key，旧密文照常解得开）：

```bash
docker compose exec xingcha python -c \
  "from pathlib import Path; from xingcha.crypto import Keyring; Keyring.load(Path('/data/secret.key')).rotate()"
docker compose restart xingcha
```

备份密钥环时**不要和数据库放同一个包**——那等于让加密对「备份泄露」这个最现实的
威胁提供零保护。

---

## 契约要真的改怎么办

先别改。[CONTRACT.md](CONTRACT.md) 里每一条都标了**演进规则**，绝大多数需求可以用
「加」而不是「改」来满足：

- 新端点 → 落在 `/v1/xc/*`（该前缀从第一天起就永不反代）
- 新响应字段 → 加进 `x_xingcha`
- 新错误情形 → 新增一个 `type`，不改既有的
- 新哈希算法 → 新 scheme 数字，旧 scheme 的校验分支永不删除

确实无路可走时，走 `X-Xingcha-Contract` 协商：契约号 +1，两个版本并行服务一段时间，
让调用方按自己的节奏迁移。**不要直接改常量**——那会让黄金测试变红，而它变红正是
在提醒你这件事。
