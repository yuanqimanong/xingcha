# 星槎 Xīngchá

> **星槎**（xīng chá）——《博物志》载有人乘槎溯河，直抵天河；明代费信随郑和下西洋，
> 著《星槎胜览》。一条渡海之筏：把提示词渡成可被调用的服务。

自部署的轻量 Agent 控制面。把 `base_url` 指向它、填一把 `sk-xc-` 密钥，业务代码就能
调用任意 OpenRouter 模型——**代理不进代码**。

```python
from openai import OpenAI

client = OpenAI(base_url="https://xc.example.com/v1", api_key="sk-xc-1-...")

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
| docker compose + Caddy 自动 TLS | ✅ |
| Agent（提示词 → 可调用的 model id） | v0.2 |
| 结构化输出保证（四档） | v0.2 / v0.3 |
| 导出 bundle · 配额 · 多用户 | v0.3 / v0.4 |

排期与设计依据见 [docs/开发计划.md](docs/开发计划.md)。

---

## 部署

见 [deploy/README.md](deploy/README.md)。简短版：

```bash
git clone git@github.com:yuanqimanong/xingcha.git
cd xingcha/deploy && ./deploy.sh     # 首次会生成 .env 并提示填写
```

一台 1C1G 的 VPS 足够。两个容器（xingcha + caddy），一个 SQLite 文件，没有
Postgres / Redis / 消息队列。

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

CI 里有一条特别的断言：在 `ALL_PROXY` 指向黑洞的环境下跑全套测试。这是
「代理不进代码」这个承诺的唯一自动化保证——星槎自建的 HTTP 客户端一律
`trust_env=False`，不继承机器级代理。

---

## 文档

| | |
|---|---|
| [docs/开发计划.md](docs/开发计划.md) | 排期、契约冻结清单、公网准入清单。**唯一权威** |
| [docs/CONTRACT.md](docs/CONTRACT.md) | 对外契约（由常量生成） |
| [deploy/README.md](deploy/README.md) | 部署 runbook |
| [docs/前期调研.md](docs/前期调研.md) | 立项调研（部分结论已被实机探测推翻，见开发计划 §2） |
| [docs/代码架构实现.md](docs/代码架构实现.md) | 早期架构设计（同上） |

---

## 许可

AGPL-3.0-only
