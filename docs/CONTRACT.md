<!-- 本文件由 `python -m xingcha.contract_doc` 从 src/xingcha/contract.py 生成。
     不要手工编辑：改动会被下次生成覆盖，而且 CI 会因为与常量不一致而变红。 -->

# 星槎对外契约 v1

调用方手里只有三样东西：`base_url`、一把 `sk-xc-` key、一个 `model` 字符串。
本文列出的每一条都对应其中一环——**上线后只能加、不能改**。

想改动其中任何一条，先读 [开发计划](开发计划.md) §3.12 的契约号协商流程。
直接改常量会让 `tests/test_contract_frozen.py` 变红，那不是测试坏了。

| | |
|---|---|
| 契约版本 | **v1** |
| 协商方式 | `X-Xingcha-Contract` 请求/响应双向头 · `GET /version` |
| 能力位 | `agents` · `passthrough` · `quota` · `streaming_agents` · `streaming_passthrough` · `structured_output` |


## 1 · 路径归属

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

## 2 · 鉴权与 token

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

## 3 · model 命名空间

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

## 4 · GET /v1/models

| 项 | 值 |
|---|---|
| Agent 行 | `owned_by=xingcha`，`id` = slug |
| 上游行 | `owned_by=openrouter`，`id` = 上游原始 id |
| 顺序 | Agent 行在前，上游行在后，按 id 去重且 Agent 优先 |
| 过滤 | `?owned_by=xingcha\|openrouter` |
| 上游拉取失败 | stale-while-error：返回上次成功快照，标 catalog_stale=true 与 fetched_at |

> 顺序必须冻结：部分客户端取 `data[0]` 当默认模型。
> 降级语义必须冻结：客户端会缓存这个列表并把 id 写进会话配置，> 一次上游抖动若让接口静默少返回上游模型，用户配置会被抹掉。

## 5 · 请求字段三态

| 态 | 字段 |
|---|---|
| **honor**（生效） | `frequency_penalty` · `logit_bias` · `max_completion_tokens` · `max_tokens` · `messages` · `model` · `presence_penalty` · `seed` · `stop` · `stream` · `stream_options` · `temperature` · `top_p` |
| **ignore**（接受但永久无语义） | `metadata` · `n` · `store` · `user` |
| **reject**（400 `param_unsupported`） | `function_call` · `functions` · `max_retries` · `response_format` · `retries` · `session_id` · `tool_choice` · `tools` · `usage_limits` |

**元规则**：列入 ignore 的字段**永久无语义，永不 honor**。需要新语义必须用新字段名。
尤其 `user`——它是 OpenAI 语义里天然的租户位，但在星槎里永久只作日志维度，**租户归属永远只来自 token**。

**演进规则**：reject 表只能缩小；永不把字段从 honor/ignore 移入 reject。

## 6 · 响应形状

| 项 | 值 |
|---|---|
| 扩展字段唯一落点 | `x_xingcha`（形状版本 v1），响应体除此之外不加任何非 OpenAI 键 |
| `message.content` | 永远是字符串；结构化输出是 `json.dumps(dict, ensure_ascii=False)` |
| 金额类型 | 字符串形式的 Decimal，或 null（`null` = 无法定价，与真实的 0 费用可区分） |
| `usage` 口径 | 整轮累计，含全部 schema 重试与工具往返产生的 token 与费用 |
| 失败响应带 usage | 是（429 / 422 也带，否则失败 run 的花费不可见） |
| SSE 帧序列 | role → content → finish → summary → done |
| SSE 终止 | `data: [DONE]` |

> `usage` 口径必须冻结：一次 200 背后可能有 `1 + retries` 次模型调用。
> 事后「修正」成只报最后一次，会让所有基于 usage 的账单核对、配额聚合与成本看板
> 同时改变口径——那是无法回退的数值毁约。

## 7 · 错误契约

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

## 8 · 裸模型直通

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

## 9 · 运行护栏

| 项 | 值 |
|---|---|
| 请求体上限 | 8 MB → 413（**事后调小是破坏性变更**） |
| schema 上限 | 64 KB · 深度 8 · 字段 120 · enum 200 |
| schema 禁用关键字 | `pattern` · `patternProperties`（ReDoS：一条 `(a+)+$` 就能打满一核，而整个服务是单进程） |
| `$ref` 限制 | 只允许 `#/` 开头（远程 `$ref` 是校验期 SSRF），并传入空 registry |
| worker 数 | 1，**启动时断言** |
| journal_mode | `wal`，**启动时断言，否则拒绝启动** |

## 10 · 数据目录与权限

| 项 | 值 |
|---|---|
| 数据库 | `data/xingcha.db` |
| 密钥环 | `data/secret.key`（MultiFernet，每行一把，首行为当前加密 key） |
| 备份 | `data/backups/`（`VACUUM INTO`，**不含密钥环**） |
| 权限 | 目录 700 · 文件 600 · umask 77 |

密钥环缺失**而库里已有密文** → **拒绝启动**。静默重新生成会让 setting 表的
上游 key 永久解不开，且当时不报任何错。这是一扇单向门。

上游 key 来源优先级：`setting` 表（Fernet 加密）> `XINGCHA_OPENROUTER_API_KEY`（仅首次启动导入一次并告警）。

## 11 · 计量

| 费用来源（`cost_source`） | `genai_prices` · `openrouter_catalog` · `unknown` · `upstream` |
|---|---|
| 输出保证档（`tier`） | `T1` · `T1P` · `T2` · `T3` |
| 判档依据 | 上游 catalog 的 `structured_outputs`（**不能看 `response_format`**——两者不等价，混用会把 T2 误判成 T1） |

四态与四档从第一天就写进数据库的 CHECK 约束，尽管 v1 只实现 T2 与前三种来源。
没预留的话，补齐时就是一次需要重建表的迁移。

