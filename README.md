# 星槎 Xīngchá

> **星槎**（xīng chá）——《博物志》载有人乘槎溯河，直抵天河；明代费信随郑和下西洋，
> 著《星槎胜览》。一条渡海之筏：把提示词渡成可被调用的服务。

自部署的轻量 Agent 控制面。把 `base_url` 指向它、填一把 `sk-xc-` 密钥，业务代码就能
调用任意 OpenRouter 模型——**代理不进代码**。

```python
from openai import OpenAI

client = OpenAI(base_url="https://192.168.1.10:8443/v1", api_key="sk-xc-1-...")

# 裸模型直通
client.chat.completions.create(model="openai/gpt-5", messages=[...])

# Agent：model 换成它的标识，200 即保证符合你定义的 JSON Schema
client.chat.completions.create(model="extract", messages=[...])
```

一台 1C1G 的 VPS 足够。**一个容器、一个 compose 文件、一个 SQLite 文件**，没有
Postgres / Redis / 消息队列。

---

## 部署

### Linux（有 docker）

```bash
git clone git@github.com:yuanqimanong/xingcha.git
cd xingcha

./deploy/edge/edge start     # 1. 起网关，提供 HTTPS（还要装一次根证书，见 CADDY.md）
./deploy/linux/xc start      # 2. 部署星槎（首次会生成 .env 并停下来提示填写）
```

日常三条命令：

| | |
|---|---|
| `./deploy/linux/xc start` | 重新构建代码并启动，`data` 不动 |
| `./deploy/linux/xc update` | 拉代码 + 重新构建启动 |
| `./deploy/linux/xc redeploy` | 清空 `data` 从零开始（会问一次 yes） |

### 没装 docker 的机器（Windows，或干净的 Linux）

双击 `deploy\windows\xc.bat`，或 `uv run xingcha serve`——用 uv 在本机直接起进程，
不打镜像。网关是同一个（`deploy/edge/`，Windows 双击 `edge.bat`）。

细节见 [deploy/README.md](deploy/README.md)。

### 关于网关

`.env` 里 `XINGCHA_GATEWAY=edge` 时，星槎那个端口被强制只绑 `127.0.0.1`，
**对外只有网关上的 HTTPS 一条路**；`xc start` 会在启动前检查网关在不在。

留空则星槎自己发布端口，**明文 HTTP**——密码与 `sk-xc-` 裸传，而且 docker 发布的
端口走 `DOCKER-USER` 链、**绕过 ufw**，所以默认只绑回环。

网关是 `deploy/edge/` 里的一个 Caddy 单文件：一个可执行文件加一份配置，没有 docker，
一台机器一个（根证书每台设备只装一次）。见 [deploy/edge/CADDY.md](deploy/edge/CADDY.md)。

---

## 用法

### 后台

浏览器打开 `https://<地址>:8443/admin`。首次访问会让你设一个管理员密码。

| 页 | 做什么 |
|---|---|
| 上游 | 填 OpenRouter key，或切到别的供应商 / 自建中转。**同一时刻只有一个出口** |
| 密钥 | 签发 `sk-xc-` 给调用方。明文只显示一次，库里只存哈希 |
| Agent | 把提示词 + 输出约定配成一个可调用的 `model` 标识 |
| 配额 | 给账号 / 某把密钥 / 某个 Agent 设金额或次数上限 |
| 调用记录 | 次数、费用与错误（**不存消息内容**） |
| 设置 | 后台密码、调用追踪（OTLP）上报地址 |

### Agent 的四档输出保证

结构化 Agent 保证 200 响应的 `content` 是符合你那份 JSON Schema 的 JSON 文本。
四档的区别是**怎么保证**与**代价**：

| 档 | 做法 | 代价 |
|---|---|---|
| T1 | 上游解码时就不让模型写出不合 schema 的 token | 只有部分模型支持；**可选字段会被提升为必填**，且格式约束会削弱推理（对齐税） |
| T2 | 模型自由作答，服务端拿 schema 校验，不合规打回重写 | 违规时多调几次，最坏 1+重试次数 倍 |
| T1+ | 先不带任何格式约束自由推理，再单独调一次只做格式化 | **两次模型调用**，约两倍的钱，慢一倍 |
| T3 | schema 只写进提示词，**输出不做校验** | 没有任何保证，字段缺了得调用方自己兜 |

不填 schema 就是纯文本，`x_xingcha.tier` 报 `none`——它不是一个档，是"不适用"。

需要原生支持的档碰上不支持的模型会**自动降级到 T2**，保存时会明说降了。
上游没有 tools 通道时（如 DeepSeek 思考模式），把 T2 的 schema 送达方式换成提示词。

**结构化 Agent 不支持 `stream=true`**，返回 400 `stream_unsupported`。流一半的 JSON
无法被安全解析，所以这里选择报错而不是在服务端缓冲完整输出再一次性吐出去。
要流式就用纯文本 Agent。

### 导出

Agent 编辑页的「导出」给你一个目录：`agent.yaml` 是标准的 pydantic-ai AgentSpec
（不是私有格式），`run.py` 零星槎依赖。改完能用 `xingcha agent apply` 导回来。

### 客户端兼容

已用真实 `openai` Python SDK 3.7.0 在 CI 里逐条验证：`models.list()` /
`models.retrieve()` / 裸模型 / Agent / 流式 / 错误分派。`/v1` 下所有非自有路径
**字节级反代**到上游，所以 OpenRouter 有的能力星槎都有。

业务代码要改的就是两行：

```python
from openai import OpenAI
client = OpenAI(base_url="https://<地址>:8443/v1", api_key="sk-xc-1-...")
```

两个已知的坑：

- **机器上设了 socks 代理**（`ALL_PROXY=socks5://...`）时，openai SDK 在构造阶段就抛
  `ImportError: ... 'socksio' package is not installed`。这跟星槎无关。而指向星槎之后
  你本来就不需要那个代理了——去掉它即可；非要留就给 SDK 传
  `http_client=httpx.Client(trust_env=False)`。
- **浏览器里跑的客户端**（Open WebUI 一类）要 CORS。星槎默认不发任何 CORS 头，
  放开：`XINGCHA_CORS_ORIGINS=https://webui.example.com`。

Cherry Studio / Continue / Cursor 这类桌面与 IDE 客户端**没有实测过**——它们走的
两个端点已被真实 SDK 验证，但依据不等于验证。

---

## 运维

下面几条要直接对容器说话：

```bash
DC="docker compose -f deploy/linux/docker-compose.yml --env-file .env"
```

### 升级

```bash
./deploy/linux/xc update
```

拉代码 → 重新构建 → 换容器。容器启动时按顺序做：修数据目录权限 → `VACUUM INTO`
备份（只在真要迁移时）→ `alembic upgrade head` → 校验密钥环 → 断言
`journal_mode=wal` → 预热模型目录。**每一条断言失败都是拒绝启动，而不是警告。**

中断 1–2 秒。已完成的请求不受影响，已签发的密钥永不失效，**正在跑的长请求会被切断**
（`stop_grace_period: 30s`；设成和 `request_timeout` 一样长的话每次升级要等 10 分钟）。

调用方不会被打断，靠三件事：契约冻结、迁移是 expand-contract 的（一次升级**只允许加**）、
迁移前自动备份。

### 回滚

```bash
# 只回代码（新版本没加迁移时，旧代码跑在新库上是安全的）
git checkout <上一个 commit> && ./deploy/linux/xc start

# 代码 + schema 都要回
$DC exec xingcha xingcha db downgrade <目标 revision> --yes
git checkout <上一个 commit> && ./deploy/linux/xc start

# 数据本身出问题
$DC exec xingcha ls /data/backups
$DC exec xingcha xingcha db restore /data/backups/xingcha-<时间戳>.db --yes
```

`downgrade` 与 `restore` 都会先备份；`restore` 前跑一次 `PRAGMA integrity_check`。

升级前想在真实数据的副本上演练（空库上的 `upgrade` 通过，证明不了有真实数据时也无感）：

```bash
$DC exec xingcha xingcha db backup --tag pre-upgrade-drill
cp data/backups/xingcha-<时间戳>-pre-upgrade-drill.db /tmp/drill.db
XINGCHA_DATA_DIR=/tmp/drill-dir xingcha db upgrade
```

### 密钥环

`data/secret.key` 是 MultiFernet 密钥环（每行一把，首行为当前加密用的）。
**它丢了，`setting` 表里的上游 key 就永久解不开**，而星槎在这种情况下拒绝启动，
不静默重新生成。轮换是纯加法（在文件头插一行新 key，旧密文照常解得开）：

```bash
$DC exec xingcha python -c \
  "from pathlib import Path; from xingcha.crypto import Keyring; Keyring.load(Path('/data/secret.key')).rotate()"
```

备份密钥环时**不要和数据库放同一个包**。

### 体检

```bash
$DC exec xingcha xingcha doctor    # 权限、schema、磁盘、代理环境变量
```

---

## 对外契约

上线之后 **key 与调用方式永不改变**：路径归属、令牌格式、`model` 命名空间、响应形状、
错误码、SSE 帧序列全部在 [CONTRACT.md](CONTRACT.md) 里冻结，此后只能加、不能改。

那份文档由 `src/xingcha/contract.py` 的常量**生成**而不是手写，并有一套黄金测试锁着：
任何改动闭集的提交都会让 CI 变红。那不是测试坏了，是在提醒你正在做一次破坏性变更。

绝大多数需求可以用「加」满足：新端点落在 `/v1/xc/*`（该前缀永不反代）、新响应字段加进
`x_xingcha`、新错误情形新增一个 `type`、新哈希算法用新 scheme 数字。确实无路可走时走
`X-Xingcha-Contract` 协商：契约号 +1，两个版本并行一段时间。

---

## 本地开发

代码怎么分层、一次调用怎么走、哪些约束由测试机械地守着——见
[ARCHITECTURE.md](ARCHITECTURE.md)。

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -e . --group dev

.venv/bin/python -m pytest          # 全套测试，离线可跑，不需要任何 API key
.venv/bin/ruff check src tests
.venv/bin/pyright
```

LLM 相关行为用 pydantic-ai 的 `FunctionModel` / `TestModel` 构造，上游用一个本地假服务器。

CI 里有两层别处看不到的断言：

- **代理指黑洞时再跑一遍全套测试**——「代理不进代码」的唯一自动化保证。星槎自建的
  HTTP 客户端一律 `trust_env=False`。
- **构建镜像并真的把整栈起起来**——断言容器 healthy、默认只绑回环、`/v1` 无凭据 401、
  纯 HTTP 下 cookie 不带 `Secure`。这类跨文件问题不会让任何单测变红。

---

## 许可

AGPL-3.0-only
