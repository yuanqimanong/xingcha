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

# Agent（v0.2）：model 换成 slug，200 即保证符合你定义的 JSON Schema
client.chat.completions.create(model="extract", messages=[...])
```

---

## 现在能做什么

| | 状态 |
|---|---|
| 裸模型透明直通（含流式） | ✅ |
| `GET /v1/models` · retrieve-model | ✅ |
| 令牌签发 / 吊销 / 速率限制 | ✅ |
| 调用记录与费用预估 | ✅ |
| Web 管理后台 | ✅ |
| 一条命令部署（单容器 docker compose） | ✅ |
| Agent（提示词 → 可调用的 model id） | ✅ |
| 结构化输出保证：**四档全实现**（T1 / T2 / T1+ / T3） | ✅ |
| schema 字段命名建议 | ✅ |
| 导出 bundle（干净环境验收通过） | ✅ |
| 配额 · 多用户 · 真流式 · 上游费用对账 | v0.4 |

排期与设计依据见 [docs/开发计划.md](docs/开发计划.md)。

---

## 部署

见 [deploy/README.md](deploy/README.md)。简短版：

```bash
git clone git@github.com:yuanqimanong/xingcha.git

# 1. 先起网关：deploy/edge/ 里的 Caddy 单文件（下载 + 起来 + 装根证书，
#    三条命令，全在 deploy/edge/CADDY.md）

# 2. 再部署 xingcha（首次会生成 .env 并停下来提示填写）
cd xingcha && ./deploy/linux/xc start
```

日常就三条：

```bash
./deploy/linux/xc start      # 重新构建代码并启动，data 不动
./deploy/linux/xc update     # 拉代码 + 重新构建启动
./deploy/linux/xc redeploy    # 清空 data 从零开始（会问一次 yes）
```

**没装 docker 的机器**（Windows，或干净的 Linux）走另一条：双击
`deploy\windows\xc.bat` 或 `uv run xingcha serve`，用 uv 在本机直接起进程。
**网关是同一个**（`deploy/edge/`，Windows 双击 `edge.bat`）。为什么这么分见
[deploy/README.md](deploy/README.md)。

一台 1C1G 的 VPS 足够。**一个容器、一个 compose 文件、一个 SQLite 文件**，没有
Postgres / Redis / 消息队列。

**挂上网关之后对外只有一条路：网关上的 HTTPS。** xingcha 那个端口被强制只绑
`127.0.0.1`，局域网上连不到。不挂网关时它是明文（密码与 `sk-xc-` 裸传），而且
docker 发布的端口走 `DOCKER-USER` 链、**绕过 ufw**。两条并存的真正问题是它们的
安全性质不同，而人只会记住能打开的那一个。

代价是**网关成了硬依赖**，`./deploy/linux/xc start` 会在启动前检查它在不在。
网关是 `deploy/edge/` 里的一个 Caddy 单文件——一个可执行文件加一份配置，没有 docker，
一台机器一个（于是根证书只需在每台设备装一次），文档见
[deploy/edge/CADDY.md](deploy/edge/CADDY.md)。

---

## 对外契约

上线之后 **key 与调用方式永不改变**：所有对外可见的东西——路径归属、令牌格式、
`model` 命名空间、响应形状、错误码、SSE 帧序列——都在
[docs/CONTRACT.md](docs/CONTRACT.md) 里冻结，此后只能加、不能改。

那份文档由 `src/xingcha/contract.py` 的常量**生成**而不是手写，并有一套黄金测试
锁着：任何改动闭集的提交都会让 CI 变红。那不是测试坏了，是在提醒你正在做一次
破坏性变更。

---

## 本地开发

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -e . --group dev

.venv/bin/python -m pytest          # 全套测试，离线可跑
.venv/bin/ruff check src tests
.venv/bin/pyright
.venv/bin/xingcha doctor            # 体检：权限、schema、磁盘、代理环境变量
```

测试**不需要**任何 API key，也不需要外网：LLM 相关行为用 pydantic-ai 的
`FunctionModel` / `TestModel` 构造，上游用一个本地假服务器。

CI（`.github/workflows/ci.yml`）里有一条特别的断言：在 `ALL_PROXY` 指向黑洞的
环境下**再跑一遍**全套测试。这是「代理不进代码」这个承诺的唯一自动化保证——星槎
自建的 HTTP 客户端一律 `trust_env=False`，不继承机器级代理。

CI 里还有一层**浏览器端到端**（42 条，Playwright + 系统的 chromium）：起真服务、
点真按钮、读真控制台。它是唯一能证明"页面在浏览器里真能用"的一层——CSP 挡掉内联
脚本那次，ASGI 层的全套测试是绿的，而复制密钥按钮无反应、危险操作的二次确认根本
不弹。本机跑：`pytest -m browser`；跳过：`pytest -m "not browser"`。

CI 还会构建镜像并**真的把整栈起起来**，断言容器 healthy、默认只绑回环、
`/healthz` 通、`/v1` 无凭据 401、以及**纯 HTTP 下 cookie 不带 `Secure`**
（带了浏览器就会丢掉它，症状是"密码输对却一直跳回登录页"，而服务端日志显示
登录成功）。这一步存在是因为这类跨文件问题不会让任何单测变红——症状只在真的
`docker compose up` 时出现。

---

## 文档

| | |
|---|---|
| [docs/开发计划.md](docs/开发计划.md) | 排期、契约冻结清单、公网准入清单。**唯一权威** |
| [docs/CONTRACT.md](docs/CONTRACT.md) | 对外契约（由常量生成） |
| [deploy/README.md](deploy/README.md) | 部署 runbook |
| [docs/客户端兼容.md](docs/客户端兼容.md) | 客户端兼容矩阵（严格区分已验证与未验证） |
| [docs/UPGRADE.md](docs/UPGRADE.md) | 升级与回滚 |
| [docs/前期调研.md](docs/前期调研.md) | 立项调研（部分结论已被实机探测推翻，见开发计划 §2） |
| [docs/代码架构实现.md](docs/代码架构实现.md) | 早期架构设计（同上） |

---

## 许可

AGPL-3.0-only
