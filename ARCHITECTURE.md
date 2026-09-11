# 架构

代码怎么分层、一次调用怎么走、哪些约束由测试机械地守着。

> 对外契约（路径、令牌格式、响应形状、错误码）在 [CONTRACT.md](CONTRACT.md)，
> 部署与运维在 [README.md](README.md)。这里只讲**内部结构**。

---

## 分层

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

不属于任何层的顶层模块——`app`（装配点）、`cli`（装配点）、`config`、`crypto`、
`errors`、`bootstrap`、`contract_doc`——允许 import 任何层。它们登记在
`tests/test_layering.py` 的 `UNLAYERED` 白名单里。

**方向由 `tests/test_layering.py` 逐文件断言**，绝对与相对两种 import 写法都认。
新加一个顶层模块而不登记，会被 `test_every_module_is_placed` 直接拦下——
守卫漏查比没有守卫更糟，它还挂着一盏绿灯。

### 为什么方向比"能不能跑"重要

反向 import 不会立刻坏事。它的代价在**以后**：

* `core` 一旦依赖 `services`，导出 bundle 那条「零星槎依赖」的卖点就开始漏；
* `contract` 一旦依赖任何东西，"契约是实现的约束"就倒挂成"契约跟着实现走"，
  而这一步不可逆——改实现从此会改契约。

这类退化每次只退一小步，且每一步都有当时看来合理的理由。所以要机械地拦。

---

## 一次调用怎么走

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

---

## 几个不好从代码里读出来的决定

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

---

## 目录

```
src/xingcha/
├── contract.py         对外承诺的常量闭集（CONTRACT.md 由它生成）
├── contract_doc.py     ↑ 的渲染器：python -m xingcha.contract_doc
├── app.py              FastAPI 装配与启动序列（每一步都可能拒绝启动）
├── config.py           环境变量 → Settings
├── crypto.py           MultiFernet 密钥环
├── errors.py           统一错误信封（OpenAI 形状）
├── bootstrap.py        serve 与 CLI 共用的最小启动准备
│
├── db/                 表定义、引擎 PRAGMA、alembic 迁移
├── obs/                OTel trace
├── core/               领域内核，见下
├── services/           用例编排，见下
├── api/                /v1
├── web/                /admin
└── cli/                命令行（命令集是闭集，契约 §3.13）
```

### `core/` —— 不碰数据库、不认识 HTTP

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

### `services/` —— 用例编排，不认识 `Request`

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

### `api/` —— `/v1`

**注册顺序有意义**：自有路径必须在 catch-all 直通之前，否则会被吞掉。

| | |
|---|---|
| `v1.py` | 装配。顺序在这里 |
| `normalize.py` | 路径归一化中间件，必须在路由之前 |
| `deps.py` | 请求级依赖：鉴权、限流、上下文 |
| `openai_compat.py` | `/v1/models` 与 `/v1/chat/completions` |
| `passthrough.py` | 其余全部字节级反代。**刻意做得很笨**：不解析任何东西 |
| `runlog_mw.py` / `sse.py` | 记账链路（两条路径共用）；流式响应 |

### `web/admin/` —— `/admin`

一页一个模块，各自带一个 `router`，由 `web/admin/__init__.py` 按顺序接起来。

跨页共用三件：`security.py`（会话 / CSRF / 同源 / 安全头）、`render.py`
（模板渲染的唯一出口）、`runs.py`（调用记录的查询与聚合，三页共用）。

**`agent_trial` 的路由必须先于 `agents` 注册**：后者有 `/agents/{slug}`，
会把 `/agents/model-report` 这类固定路径吞掉，而症状是页面上出现
"未知的 Agent：model-report"，看起来像数据问题。

### `cli/` —— 一个命令组一个模块

`_app.py` 持有 `Typer` 对象（打破 `__init__` ↔ 命令模块的环），
`_ui.py` 管输出、中文表格对齐与运维类异常的收场。

---

## 由测试守着的约束

这些不是"建议"，违反了 CI 会红：

| 约束 | 守卫 |
|---|---|
| 依赖方向单向、无环 | `tests/test_layering.py` |
| 对外契约闭集不变 | `tests/test_contract_frozen.py`（黄金测试） |
| `CONTRACT.md` 与 `contract.py` 一致 | 同上 |
| 代理指黑洞时全套测试仍通过 | CI：「代理不进代码」的唯一自动化保证 |
| 容器 healthy、默认只绑回环、`/v1` 无凭据 401 | CI：真起整栈 |
| 导出物零星槎依赖 | `pytest -m slow`：建干净 venv 真跑 |

契约测试变红时**不是测试坏了**，是在提醒你正在做一次破坏性变更。
