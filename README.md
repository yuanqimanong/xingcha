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

./deploy/edge/edge start     # 1. 起网关，提供 HTTPS（还要装一次根证书，见 deploy/README.md）
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
一台机器一个（根证书每台设备只装一次）。见 [deploy/README.md](deploy/README.md)。

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
  "from pathlib import Path; from xingcha.foundation.crypto import Keyring; Keyring.load(Path('/data/secret.key')).rotate()"
```

备份密钥环时**不要和数据库放同一个包**。

### 体检

```bash
$DC exec xingcha xingcha doctor    # 权限、schema、磁盘、代理环境变量
```

---

## 对外契约

上线之后 **key 与调用方式永不改变**：路径归属、令牌格式、`model` 命名空间、响应形状、
错误码、SSE 帧序列全部冻结在下面这一节里，此后只能加、不能改。

那一节由 `src/xingcha/contract/` 的常量**生成**而不是手写，并有一套黄金测试锁着：
任何改动闭集的提交都会让 CI 变红。那不是测试坏了，是在提醒你正在做一次破坏性变更。

<!-- BEGIN GENERATED · python -m xingcha.contract.doc · 不要手工编辑 -->

调用方手里只有三样东西：`base_url`、一把 `sk-xc-` key、一个 `model` 字符串。
本节列出的每一条都对应其中一环——**上线后只能加、不能改**。

想改动其中任何一条，先读 [§12 演进与协商](#12--演进与协商)。
直接改常量会让 `tests/test_contract_frozen.py` 变红，那不是测试坏了。

| | |
|---|---|
| 契约版本 | **v1** |
| 协商方式 | `X-Xingcha-Contract` 请求/响应双向头 · `GET /version` |
| 能力位 | `agents` · `passthrough` · `quota` · `streaming_agents` · `streaming_passthrough` · `structured_output` |


### 1 · 路径归属

对外前缀：`/admin` · `/api/v1` · `/healthz` · `/v1`

`/v1` 下星槎自有路径是一个**闭集**，其余全部字节级反代到上游：

| 路径 | 说明 |
|---|---|
| `/v1/chat/completions` | 星槎自有 |
| `/v1/models` | 星槎自有 |
| `/v1/models/{id}` | 仅**单段**；多段（如 `/v1/models/{author}/{slug}/endpoints`）属于上游，留给反代 |
| `/v1/xc/*` | 永久保留区，从不反代。**新增自有端点只能落在这里** |
| `OPTIONS /v1/**` | 一律由星槎应答，永不反代 |

匹配前先归一化：折叠重复斜杠、去掉首尾斜杠、**大小写敏感**。

> 没有归一化会有一个上线第一天就存在的静默 bug：`GET /v1/models/` 带尾斜杠时，
> FastAPI 的 `redirect_slashes` 在 catch-all 存在时不生效，请求直接被反代出去——
> 客户端拿到 200、拿到几百个上游模型、一个 Agent 都看不到，且没有任何报错。

**演进规则**：从反代收回任意 `/v1` 路径属于破坏性变更，必须契约号 +1 并双轨服务。

### 2 · 鉴权与 token

只认 `authorization: Bearer <token>`。永不支持 query string 传 key，永不支持 `api-key` / `x-api-key` 头。

```
信封  sk-xc-<scheme>-<kid>-<secret>
正则  ^sk-xc-(?P<scheme>[1-9][0-9]{0,2})-(?P<kid>[0-9a-z]{16})-(?P<secret>[A-Za-z0-9_-]{16,86})$
kid   16 位小写字母数字，唯一查表键，不可推导
当前  scheme=1（secret 43 字符，校验 = 常量时间比较 sha256(secret)）
```

服务端永久保留校验能力的 scheme：`1`

对外**不区分** token 无效 / 禁用 / 过期，一律 `invalid_api_key`——区分等于给公网一个 token 有效性 oracle。

**演进规则**：换哈希算法 = 新 scheme 数字，旧 scheme 的校验分支永不删除，已签发 key 不重签、不失效。`kid` 长度与字符集不再变化；`secret` 长度可随 scheme 变化。

### 3 · model 命名空间

```
① 以 'xc:' 开头  → 显式命名空间：xc:agent/<slug> 或 xc:model/<上游 id>
② 否则含 '/'          → 上游裸模型 id，原样透传
③ 其余                → Agent slug；查不到即 404，绝不猜测性转发上游
```

| | |
|---|---|
| Agent slug 正则 | `^[a-z][a-z0-9]*(-[a-z0-9]+)*$` |
| 长度 | 2–48 |
| 保留字 | `admin` · `api` · `health` · `healthz` · `me` · `models` · `readyz` · `version` · `xc` |
| 保留前缀 | `xc-` |
| 上游 id 正则 | `^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+(:[A-Za-z0-9._-]+)?$` |

slug 是**全局**唯一命名空间（`agent.slug` 有 UNIQUE 约束），不是 per-user。

**演进规则**：「含 `/` 即上游」永不反转；slug 字符集只能收缩到更严（放宽会让原本 404 的字符串突然变成有效 Agent）；slug 发布后不可改名，改名走 `agent_alias` 表；per-user 命名空间只能通过新前缀引入。

### 4 · GET /v1/models

| 项 | 值 |
|---|---|
| Agent 行 | `owned_by=xingcha`，`id` = slug |
| 上游行 | `owned_by=openrouter`，`id` = 上游原始 id |
| 顺序 | Agent 行在前，上游行在后，按 id 去重且 Agent 优先 |
| 过滤 | `?owned_by=xingcha\|openrouter` |
| 上游拉取失败 | stale-while-error：返回上次成功快照，标 catalog_stale=true 与 fetched_at |

> 顺序必须冻结：部分客户端取 `data[0]` 当默认模型。
> 降级语义必须冻结：客户端会缓存这个列表并把 id 写进会话配置，> 一次上游抖动若让接口静默少返回上游模型，用户配置会被抹掉。

### 5 · 请求字段三态

| 态 | 字段 |
|---|---|
| **honor**（生效） | `frequency_penalty` · `logit_bias` · `max_completion_tokens` · `max_tokens` · `messages` · `model` · `presence_penalty` · `seed` · `stop` · `stream` · `stream_options` · `temperature` · `top_p` |
| **ignore**（接受但永久无语义） | `metadata` · `n` · `store` · `user` |
| **reject**（400 `param_unsupported`） | `function_call` · `functions` · `max_retries` · `response_format` · `retries` · `session_id` · `tool_choice` · `tools` · `usage_limits` |

**元规则**：列入 ignore 的字段**永久无语义，永不 honor**。需要新语义必须用新字段名。
尤其 `user`——它是 OpenAI 语义里天然的租户位，但在星槎里永久只作日志维度，**租户归属永远只来自 token**。

**演进规则**：reject 表只能缩小；永不把字段从 honor/ignore 移入 reject。

### 6 · 响应形状

| 项 | 值 |
|---|---|
| 扩展字段唯一落点 | `x_xingcha`（形状版本 v1），响应体除此之外不加任何非 OpenAI 键 |
| `message.content` | 永远是字符串；结构化输出是 `json.dumps(dict, ensure_ascii=False)` |
| 金额类型 | 字符串形式的 Decimal，或 null（`null` = 无法定价，与真实的 0 费用可区分） |
| `usage` 口径 | 整轮累计，含全部 schema 重试与工具往返产生的 token 与费用 |
| 失败响应带 usage | 是 —— `quota_exceeded` · `schema_violation` 必带，**零调用也给 0**（形状与 200 一致，调用方不必分情况） |
| SSE 帧序列 | role → content → finish → summary → done |
| SSE 终止 | `data: [DONE]` |

> `usage` 口径必须冻结：一次 200 背后可能有 `1 + retries` 次模型调用。
> 事后「修正」成只报最后一次，会让所有基于 usage 的账单核对、配额聚合与成本看板
> 同时改变口径——那是无法回退的数值毁约。

### 7 · 错误契约

`type` 是粗粒度闭集（供 SDK 分支），`code` 可更细。**两者不相等。**

| type | HTTP |
|---|---|
| `invalid_api_key` | 401 |
| `quota_exceeded` | 429 |
| `model_not_found` | 404 |
| `model_invalid` | 400 |
| `param_unsupported` | 400 |
| `stream_unsupported` | 400 |
| `request_too_large` | 413 |
| `schema_violation` | 422 |
| `agent_spec_invalid` | 400 |
| `agent_build_failed` | 500 |
| `upstream_error` | 502 |
| `upstream_timeout` | 504 |
| `request_timeout` | 504 |
| `internal_error` | 500 |

**演进规则**：只能新增 `type`，且新值必须配一个此前未使用的语义；既有 `type` 的 HTTP 码永不改动、永不改名、永不复用于别的语义。

5xx 对外只给固定文案 + `run_id`，细节只进日志——异常文本常带完整 URL、偶尔带 header，直接回显就是一条上游 key 泄漏路径。

### 8 · 裸模型直通

| 项 | 值 |
|---|---|
| 鉴权 | **强制**，无有效 key 一律 401，绝不转发给上游 |
| 配额 | **v1 不执行**（记 run 与 token，但不拦） |
| 剥离的请求头 | `authorization` · `cf-connecting-ip` · `cf-ipcountry` · `connection` · `content-length` · `cookie` · `forwarded` · `host` · `keep-alive` · `proxy-authenticate` · `proxy-authorization` · `te` · `trailer` · `transfer-encoding` · `true-client-ip` · `upgrade` · `x-client-ip` · `x-forwarded-for` · `x-forwarded-host` · `x-forwarded-proto` · `x-real-ip` |
| 回显的响应头（**白名单**） | `cache-control` · `content-encoding` · `content-type` · `retry-after` · `x-ratelimit-limit` · `x-ratelimit-remaining` · `x-ratelimit-reset` · `x-request-id` |

> 响应头必须是白名单而不是黑名单：只剥 hop-by-hop 就逐字节透传的话，
> 上游的 `Set-Cookie` 会落在你自己的域上。

> **v1 唯一真正的钱刹车不在星槎里。** v1 不做配额，必须在 OpenRouter 侧
> 为服务端那把上游 key 单独设一个低额信用上限。

### 9 · 运行护栏

| 项 | 值 |
|---|---|
| 请求体上限 | 8 MB → 413（**事后调小是破坏性变更**） |
| schema 上限 | 64 KB · 深度 8 · 字段 120 · enum 200 |
| schema 禁用关键字 | `pattern` · `patternProperties`（ReDoS：一条 `(a+)+$` 就能打满一核，而整个服务是单进程） |
| `$ref` 限制 | 只允许 `#/` 开头（远程 `$ref` 是校验期 SSRF），并传入空 registry |
| worker 数 | 1，**启动时断言** |
| journal_mode | `wal`，**启动时断言，否则拒绝启动** |

### 10 · 数据目录与权限

| 项 | 值 |
|---|---|
| 数据库 | `data/xingcha.db` |
| 密钥环 | `data/secret.key`（MultiFernet，每行一把，首行为当前加密 key） |
| 备份 | `data/backups/`（`VACUUM INTO`，**不含密钥环**） |
| 权限 | 目录 700 · 文件 600 · umask 77 |

密钥环缺失**而库里已有密文** → **拒绝启动**。静默重新生成会让 setting 表的
上游 key 永久解不开，且当时不报任何错。这是一扇单向门。

上游 key 来源优先级：`setting` 表（Fernet 加密）> `XINGCHA_OPENROUTER_API_KEY`（仅首次启动导入一次并告警）。

### 11 · 计量

| 费用来源（`cost_source`） | `genai_prices` · `openrouter_catalog` · `unknown` · `upstream` |
|---|---|
| 输出保证档（`tier`） | `T1` · `T1P` · `T2` · `T3` · `none` |
| 判档依据 | 上游 catalog 的 `structured_outputs`（**不能看 `response_format`**——两者不等价，混用会把 T2 误判成 T1） |

四态与四档从第一天就写进数据库的 CHECK 约束，尽管 v1 只实现 T2 与前三种来源。
没预留的话，补齐时就是一次需要重建表的迁移。

### 12 · 演进与协商

上面每一节的「演进规则」讲的是**那一条**怎么加。这一节讲的是加不动的时候怎么办。

**绝大多数需求可以用「加」满足**，四条路：

| 新端点 | 落在 `/v1/xc/*`——该前缀永不反代 |
|---|---|
| 新响应字段 | 加进 `x_xingcha` 扩展块（形状版本 1），不动 OpenAI 形状 |
| 新错误情形 | 新增一个 `type`，既有的永不改名、永不复用 |
| 新哈希算法 | 新 scheme 数字，旧 scheme 的校验分支永不删除 |

**确实无路可走时才动契约号**：

1. `X-Xingcha-Contract` 请求头带旧版本的调用方继续按旧行为服务；
2. 契约号 +1，两个版本并行一段时间；
3. `GET /version` 与响应头同时公布当前号，调用方可以据此分支。

改常量会让 `tests/test_contract_frozen.py` 变红。**那不是测试坏了**——
它是在提醒你正在做一次破坏性变更，先走完上面三步。

改完记得重新生成本节：`python -m xingcha.contract.doc`。

<!-- END GENERATED -->

---

## 架构

代码怎么分层、一次调用怎么走、哪些约束由测试机械地守着。

### 分层

依赖单向，低层不 import 高层：

```
contract  →  db  →  obs  →  core  →  services  →  api  →  web
```

| 层 | 是什么 | 不是什么 |
|---|---|---|
| `contract` | 对外承诺的常量闭集。**不 import 任何东西** | 不是配置：这里的值改一个就是毁约 |
| `db` | 表定义、引擎、迁移 | 不含业务判断 |
| `obs` | OTel trace 装配 | 不认识 Agent |
| `core` | 领域内核：构造 Agent、输出保证、schema 护栏、导出 | **不碰数据库会话，不认识 HTTP** |
| `services` | 用例编排：增删改查、配额、限流、执行一次调用 | 不认识 FastAPI 的 `Request` |
| `api` | `/v1`：OpenAI 兼容 + 直通反代 | 不写业务规则 |
| `web` | `/admin`：管理后台 | 不被任何人 import |

不属于任何层的那几个——`app`、`cli`（装配点）、`config`、`bootstrap`，以及
`foundation/`（密钥环与错误信封，谁都要用，放进任何一层都会逼出反向依赖）——允许
import 任何层。它们登记在 `tests/test_layering.py` 的 `UNLAYERED` 白名单里。

**方向由 `tests/test_layering.py` 逐文件断言**，绝对与相对两种 import 写法都认。
新加一个顶层模块而不登记，会被 `test_every_module_is_placed` 直接拦下——
守卫漏查比没有守卫更糟，它还挂着一盏绿灯。

#### 为什么方向比"能不能跑"重要

反向 import 不会立刻坏事。它的代价在**以后**：

* `core` 一旦依赖 `services`，导出 bundle 那条「零星槎依赖」的卖点就开始漏；
* `contract` 一旦依赖任何东西，"契约是实现的约束"就倒挂成"契约跟着实现走"，
  而这一步不可逆——改实现从此会改契约。

这类退化每次只退一小步，且每一步都有当时看来合理的理由。所以要机械地拦。

### 一次调用怎么走

```
POST /v1/chat/completions
  │
  ├─ NormalizeV1Path ······· 折叠斜杠。不做这步，/v1/models/ 会被反代出去
  ├─ deps.authenticate ····· sk-xc- → token 行；失败即 401，上游一个字节都不发
  ├─ ratelimit ············· 按令牌的速率与并发
  ├─ classify_model ········ 这个 model 是 Agent 的 slug 还是裸模型？
  │
  ├─(Agent)─→ services/run ─→ core/builder ─→ pydantic-ai ─→ 上游
  │              └ core/guarantee 按档位保证输出符合 schema
  │
  └─(裸模型)→ api/passthrough ──────────── 字节级反代 ─────→ 上游
                 只识别路径、只嗅探用量，其余什么都不解析
  │
  └─ runlog_mw ············ 两条路径共用一条记账链路
       └ services/quota 在**检查时**就占掉次数（见下）
```

两条路径共用记账，是因为它们花的是同一把上游 key 的钱——分开记会让
「这个月一共花了多少」变成一次 UNION，而那正是最常被问的问题。

### 几个不好从代码里读出来的决定

**上游只有一个出口。** 同一时刻只有一份 `(api_key, base_url)` 生效，存在
`setting` 表里（加密）。环境变量只是**发现**来源，不是长期存放处——它会进
`docker inspect` 与 `/proc/<pid>/environ`。切换见 `web/admin/upstreams.py`。

**配额的计数在内存里，数据库只是持久记录。** 用量是异步批量落库的，
每次去查表求和会漏算刚发生的调用。且**检查时就预留**，不等调用结束——
否则 50 个并发会全部通过检查再各自 +1，限额 2 放过 50 个。
这依赖单 worker（`contract.REQUIRED_WORKERS`），多 worker 下配额会变成 N 倍。

**Agent 整块存 JSON（`AgentSpec`），不拆列。** 升级 pydantic-ai 只改
`core/builder.py` 一个文件——它是**上游版本适配的唯一集中点**，别处不解释
spec 字段的含义。代价是查询不了 spec 内部，而我们不需要。

**密钥环丢了就拒绝启动，不静默重新生成。** 静默重建会让 `setting` 表里的密文
变成永远解不开的垃圾，而服务看起来是好的——直到第一次调用上游。

### 目录

```
src/xingcha/
├── contract/           对外承诺的常量闭集（最底层，不 import 任何层）
│   ├── __init__.py     常量本身
│   └── doc.py          ↑ 的渲染器：python -m xingcha.contract.doc
├── foundation/         跨层基础件：谁都要用，所以不属于任何一层
│   ├── crypto.py       MultiFernet 密钥环
│   └── errors.py       统一错误信封（OpenAI 形状）
├── app.py              FastAPI 装配与启动序列（每一步都可能拒绝启动）
├── config.py           环境变量 → Settings
├── bootstrap.py        serve 与 CLI 共用的最小启动准备
│
├── db/                 表定义、引擎 PRAGMA、alembic 迁移
├── obs/                OTel trace
├── core/               领域内核，见下
├── services/           用例编排，见下
├── api/                /v1
├── web/                /admin
└── cli/                命令行（命令集是闭集：脚本依赖这些命令名）
```

#### `core/` —— 不碰数据库、不认识 HTTP

| | |
|---|---|
| `builder.py` | 数据库行 → 可执行 Agent。**pydantic-ai 适配的唯一集中点** |
| `guarantee.py` | 四档输出保证（T1 / T1+ / T2 / T3）与降级 |
| `schema_guard.py` | 用户提交的 JSON Schema 的安全护栏（禁远程 `$ref` 等） |
| `schema_lint.py` | schema 字段命名建议 |
| `exporter.py` | 导出 bundle：`agent.yaml` + 零星槎依赖的 `run.py` |
| `models_catalog.py` | 上游模型目录（判档与定价的主价源） |
| `upstream.py` / `urlguard.py` | 上游 HTTP 客户端（一律 `trust_env=False`）与地址校验 |
| `costsink.py` / `ids.py` | 上游实际费用的收集点；标识生成 |

#### `services/` —— 用例编排，不认识 `Request`

| | |
|---|---|
| `run.py` | 执行一次 Agent 调用：消息整形、错误映射、响应/SSE 成形 |
| `agent.py` | Agent 增删改查与版本管理 |
| `quota.py` | 三级主体 × 三种窗口的配额执行 |
| `ratelimit.py` | 按令牌的速率与并发 |
| `auth.py` | `sk-xc-` 签发与校验 |
| `runlog.py` | 调用记录与用量缓冲 |
| `setting.py` / `providers.py` / `upstream_env.py` / `trace_targets.py` | 配置、供应商、可切换上游、上报目标 |
| `websession.py` / `agent_test.py` | 后台登录会话与 CSRF；后台试运行的记录 |

#### `api/` —— `/v1`

**注册顺序有意义**：自有路径必须在 catch-all 直通之前，否则会被吞掉。

| | |
|---|---|
| `v1.py` | 装配。顺序在这里 |
| `normalize.py` | 路径归一化中间件，必须在路由之前 |
| `deps.py` | 请求级依赖：鉴权、限流、上下文 |
| `openai_compat.py` | `/v1/models` 与 `/v1/chat/completions` |
| `passthrough.py` | 其余全部字节级反代。**刻意做得很笨**：不解析任何东西 |
| `runlog_mw.py` / `sse.py` | 记账链路（两条路径共用）；流式响应 |

#### `web/admin/` —— `/admin`

一页一个模块，各自带一个 `router`，由 `web/admin/__init__.py` 按顺序接起来。

跨页共用三件：`security.py`（会话 / CSRF / 同源 / 安全头）、`render.py`
（模板渲染的唯一出口）、`runs.py`（调用记录的查询与聚合，三页共用）。

**`agent_trial` 的路由必须先于 `agents` 注册**：后者有 `/agents/{slug}`，
会把 `/agents/model-report` 这类固定路径吞掉，而症状是页面上出现
"未知的 Agent：model-report"，看起来像数据问题。

#### `cli/` —— 一个命令组一个模块

`_app.py` 持有 `Typer` 对象（打破 `__init__` ↔ 命令模块的环），
`_ui.py` 管输出、中文表格对齐与运维类异常的收场。

### 由测试守着的约束

这些不是"建议"，违反了 CI 会红：

| 约束 | 守卫 |
|---|---|
| 对外契约闭集不变、分派规则不变 | `tests/test_contract_frozen.py`（黄金测试） |
| README 的「对外契约」与 `contract/` 一致 | 同上 |
| 依赖方向单向、每个顶层模块都已登记 | `tests/test_layering.py` |
| 网关/应用端口在 6 处产物间相等 | `tests/test_deploy_artifacts.py` |
| 宿主端口默认只绑回环、网关叠加层两项齐全 | 同上 |
| 容器 healthy、`/v1` 无凭据 401 | CI：真起整栈 |

契约测试变红时**不是测试坏了**，是在提醒你正在做一次破坏性变更。

期望值写死在测试文件里，不从 `contract` 反向读取——从常量读的"测试"只能证明常量
等于它自己，改一个闭集照样绿。

> 上一轮重构把整个 `tests/` 清空了（`e298423`）。目前恢复的是上面这三份**静态守卫**：
> 不需要建库、不需要起服务、不碰网络，所以跑一遍是秒级的。尚未恢复的运行时测试有
> 配额、流式、直通反代、导出物零依赖、代理指黑洞——CI 里对应的步骤仍然注释着，
> 那几条约束当前**没有任何自动检查**。

---

## 本地开发

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
