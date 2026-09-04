"""从 :mod:`xingcha.contract` 的常量生成 ``docs/CONTRACT.md``。

**文档不手写。** 手写的契约文档一定会和代码漂移，而漂移的那一刻你就有了两份互相矛盾
的"权威"——更糟的是，人会去信文档而不是代码。这里把常量渲染成文档，并由
``tests/test_contract_doc.py`` 断言仓库里的文件与生成结果一致：改了常量却忘了重新
生成，CI 会变红。

重新生成：``python -m xingcha.contract_doc``
"""

from __future__ import annotations

from pathlib import Path

from . import contract as C

HEADER = """<!-- 本文件由 `python -m xingcha.contract_doc` 从 src/xingcha/contract.py 生成。
     不要手工编辑：改动会被下次生成覆盖，而且 CI 会因为与常量不一致而变红。 -->

# 星槎对外契约 v{version}

调用方手里只有三样东西：`base_url`、一把 `sk-xc-` key、一个 `model` 字符串。
本文列出的每一条都对应其中一环——**上线后只能加、不能改**。

想改动其中任何一条，先读 [开发计划](开发计划.md) §3.12 的契约号协商流程。
直接改常量会让 `tests/test_contract_frozen.py` 变红，那不是测试坏了。

| | |
|---|---|
| 契约版本 | **v{version}** |
| 协商方式 | `X-Xingcha-Contract` 请求/响应双向头 · `GET /version` |
| 能力位 | {features} |
"""


def _fs(items) -> str:
    return " · ".join(f"`{x}`" for x in sorted(items))


def render() -> str:
    out: list[str] = [
        HEADER.format(version=C.CONTRACT_VERSION, features=_fs(C.FEATURES)),
        "",
        "## 1 · 路径归属",
        "",
        f"对外前缀：{_fs(C.PUBLIC_PREFIXES)}",
        "",
        "`/v1` 下星槎自有路径是一个**闭集**，其余全部字节级反代到上游：",
        "",
        "| 路径 | 说明 |",
        "|---|---|",
    ]
    for p in sorted(C.OWN_V1_PATHS):
        out.append(f"| `/v1/{p}` | 星槎自有 |")
    out += [
        "| `/v1/models/{id}` | 仅**单段**；多段（如 `/v1/models/{author}/{slug}/endpoints`）"
        "属于上游，留给反代 |",
        f"| `/v1/{C.RESERVED_V1_PREFIX}/*` | 永久保留区，从不反代。**新增自有端点只能落在这里** |",
        f"| `OPTIONS /v1/**` | {'一律由星槎应答，永不反代' if C.OPTIONS_ALWAYS_OWN else '反代'} |",
        "",
        "匹配前先归一化：折叠重复斜杠、去掉首尾斜杠、**大小写敏感**。",
        "",
        "> 没有归一化会有一个上线第一天就存在的静默 bug：`GET /v1/models/` 带尾斜杠时，",
        "> FastAPI 的 `redirect_slashes` 在 catch-all 存在时不生效，请求直接被反代出去——",
        "> 客户端拿到 200、拿到几百个上游模型、一个 Agent 都看不到，且没有任何报错。",
        "",
        "**演进规则**：从反代收回任意 `/v1` 路径属于破坏性变更，必须契约号 +1 并双轨服务。",
        "",
        "## 2 · 鉴权与 token",
        "",
        f"只认 `{C.AUTH_HEADER}: {C.AUTH_SCHEME.title()} <token>`。"
        "永不支持 query string 传 key，永不支持 `api-key` / `x-api-key` 头。",
        "",
        "```",
        "信封  sk-xc-<scheme>-<kid>-<secret>",
        f"正则  {C.TOKEN_ENVELOPE_RE.pattern}",
        f"kid   {C.TOKEN_KID_LEN} 位小写字母数字，唯一查表键，不可推导",
        f"当前  scheme={C.TOKEN_SCHEME_CURRENT}"
        f"（secret {C.TOKEN_SCHEME_1_SECRET_LEN} 字符，校验 = 常量时间比较 "
        f"{C.TOKEN_SCHEME_1_ALG}(secret)）",
        "```",
        "",
        f"服务端永久保留校验能力的 scheme：{_fs(str(s) for s in C.TOKEN_SCHEMES_SUPPORTED)}",
        "",
        "对外**不区分** token 无效 / 禁用 / 过期，一律 `invalid_api_key`"
        "——区分等于给公网一个 token 有效性 oracle。",
        "",
        "**演进规则**：换哈希算法 = 新 scheme 数字，旧 scheme 的校验分支永不删除，"
        "已签发 key 不重签、不失效。`kid` 长度与字符集不再变化；`secret` 长度可随 scheme 变化。",
        "",
        "## 3 · model 命名空间",
        "",
        "```",
        f"① 以 {C.EXPLICIT_NS!r} 开头  → 显式命名空间："
        f"{C.EXPLICIT_NS}{C.EXPLICIT_KIND_AGENT}/<slug> 或 "
        f"{C.EXPLICIT_NS}{C.EXPLICIT_KIND_MODEL}/<上游 id>",
        "② 否则含 '/'          → 上游裸模型 id，原样透传",
        "③ 其余                → Agent slug；查不到即 404，绝不猜测性转发上游",
        "```",
        "",
        "| | |",
        "|---|---|",
        f"| Agent slug 正则 | `{C.SLUG_RE.pattern}` |",
        f"| 长度 | {C.SLUG_MIN_LEN}–{C.SLUG_MAX_LEN} |",
        f"| 保留字 | {_fs(C.SLUG_RESERVED)} |",
        f"| 保留前缀 | `{C.SLUG_RESERVED_PREFIX}` |",
        f"| 上游 id 正则 | `{C.UPSTREAM_MODEL_RE.pattern}` |",
        "",
        "slug 是**全局**唯一命名空间（`agent.slug` 有 UNIQUE 约束），不是 per-user。",
        "",
        "**演进规则**：「含 `/` 即上游」永不反转；slug 字符集只能收缩到更严"
        "（放宽会让原本 404 的字符串突然变成有效 Agent）；slug 发布后不可改名，"
        "改名走 `agent_alias` 表；per-user 命名空间只能通过新前缀引入。",
        "",
        "## 4 · GET /v1/models",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| Agent 行 | `owned_by={C.OWNED_BY_XINGCHA}`，`id` = slug |",
        f"| 上游行 | `owned_by={C.OWNED_BY_UPSTREAM}`，`id` = 上游原始 id |",
        "| 顺序 | "
        + (
            "Agent 行在前，上游行在后，按 id 去重且 Agent 优先"
            if C.MODELS_AGENTS_FIRST
            else "上游优先"
        )
        + " |",
        "| 过滤 | `?owned_by=xingcha\\|openrouter` |",
        "| 上游拉取失败 | "
        + (
            "stale-while-error：返回上次成功快照，标 catalog_stale=true 与 fetched_at"
            if C.MODELS_STALE_WHILE_ERROR
            else "报错"
        )
        + " |",
        "",
        "> 顺序必须冻结：部分客户端取 `data[0]` 当默认模型。",
        "> 降级语义必须冻结：客户端会缓存这个列表并把 id 写进会话配置，"
        "> 一次上游抖动若让接口静默少返回上游模型，用户配置会被抹掉。",
        "",
        "## 5 · 请求字段三态",
        "",
        "| 态 | 字段 |",
        "|---|---|",
        f"| **honor**（生效） | {_fs(C.REQUEST_HONOR)} |",
        f"| **ignore**（接受但永久无语义） | {_fs(C.REQUEST_IGNORE)} |",
        f"| **reject**（400 `param_unsupported`） | {_fs(C.REQUEST_REJECT)} |",
        "",
        "**元规则**：列入 ignore 的字段**永久无语义，永不 honor**。需要新语义必须用新字段名。",
        "尤其 `user`——它是 OpenAI 语义里天然的租户位，但在星槎里永久只作日志维度，"
        "**租户归属永远只来自 token**。",
        "",
        "**演进规则**：reject 表只能缩小；永不把字段从 honor/ignore 移入 reject。",
        "",
        "## 6 · 响应形状",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| 扩展字段唯一落点 | `{C.EXT_KEY}`（形状版本 v{C.EXT_SHAPE_VERSION}），"
        "响应体除此之外不加任何非 OpenAI 键 |",
        f"| `message.content` | {'永远是字符串' if C.CONTENT_ALWAYS_STR else '可为对象'}"
        "；结构化输出是 `json.dumps(dict, ensure_ascii=False)` |",
        f"| 金额类型 | {'字符串形式的 Decimal，或 null' if C.COST_AS_STRING else 'number'}"
        "（`null` = 无法定价，与真实的 0 费用可区分） |",
        f"| `usage` 口径 | {'整轮累计' if C.USAGE_IS_WHOLE_RUN else '单次'}，"
        "含全部 schema 重试与工具往返产生的 token 与费用 |",
        f"| 失败响应带 usage | {'是' if C.USAGE_ON_ERROR else '否'}（429 / 422 也带，"
        "否则失败 run 的花费不可见） |",
        f"| SSE 帧序列 | {' → '.join(C.SSE_FRAME_ORDER)} |",
        f"| SSE 终止 | `{C.SSE_DONE.strip()}` |",
        "",
        "> `usage` 口径必须冻结：一次 200 背后可能有 `1 + retries` 次模型调用。",
        "> 事后「修正」成只报最后一次，会让所有基于 usage 的账单核对、配额聚合与成本看板",
        "> 同时改变口径——那是无法回退的数值毁约。",
        "",
        "## 7 · 错误契约",
        "",
        "`type` 是粗粒度闭集（供 SDK 分支），`code` 可更细。**两者不相等。**",
        "",
        "| type | HTTP |",
        "|---|---|",
    ]
    for et in C.ErrorType:
        out.append(f"| `{et.value}` | {C.ERROR_HTTP_STATUS[et]} |")
    out += [
        "",
        "**演进规则**：只能新增 `type`，且新值必须配一个此前未使用的语义；"
        "既有 `type` 的 HTTP 码永不改动、永不改名、永不复用于别的语义。",
        "",
        "5xx 对外只给固定文案 + `run_id`，细节只进日志"
        "——异常文本常带完整 URL、偶尔带 header，直接回显就是一条上游 key 泄漏路径。",
        "",
        "## 8 · 裸模型直通",
        "",
        "| 项 | 值 |",
        "|---|---|",
        "| 鉴权 | "
        + (
            "**强制**，无有效 key 一律 401，绝不转发给上游"
            if C.PASSTHROUGH_REQUIRES_AUTH
            else "可选"
        )
        + " |",
        "| 配额 | "
        + ("执行" if C.PASSTHROUGH_ENFORCES_QUOTA else "**v1 不执行**（记 run 与 token，但不拦）")
        + " |",
        f"| 剥离的请求头 | {_fs(C.STRIP_REQUEST_HEADERS)} |",
        f"| 回显的响应头（**白名单**） | {_fs(C.ALLOW_RESPONSE_HEADERS)} |",
        "",
        "> 响应头必须是白名单而不是黑名单：只剥 hop-by-hop 就逐字节透传的话，",
        "> 上游的 `Set-Cookie` 会落在你自己的域上。",
        "",
        "> **v1 唯一真正的钱刹车不在星槎里。** v1 不做配额，必须在 OpenRouter 侧",
        "> 为服务端那把上游 key 单独设一个低额信用上限。",
        "",
        "## 9 · 运行护栏",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| 请求体上限 | {C.MAX_BODY_BYTES // 1024 // 1024} MB → 413（**事后调小是破坏性变更**） |",
        f"| schema 上限 | {C.SCHEMA_MAX_BYTES // 1024} KB · 深度 {C.SCHEMA_MAX_DEPTH} · "
        f"字段 {C.SCHEMA_MAX_PROPS} · enum {C.SCHEMA_MAX_ENUM} |",
        f"| schema 禁用关键字 | {_fs(C.SCHEMA_FORBIDDEN_KEYWORDS)}（ReDoS：一条 `(a+)+$` "
        "就能打满一核，而整个服务是单进程） |",
        f"| `$ref` 限制 | 只允许 `{C.SCHEMA_REF_ALLOWED_PREFIX}` 开头"
        "（远程 `$ref` 是校验期 SSRF），并传入空 registry |",
        f"| worker 数 | {C.REQUIRED_WORKERS}，**启动时断言** |",
        f"| journal_mode | `{C.REQUIRED_JOURNAL_MODE}`，**启动时断言，否则拒绝启动** |",
        "",
        "## 10 · 数据目录与权限",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| 数据库 | `data/{C.DB_FILENAME}` |",
        f"| 密钥环 | `data/{C.SECRET_FILENAME}`（MultiFernet，每行一把，首行为当前加密 key） |",
        f"| 备份 | `data/{C.BACKUP_DIRNAME}/`（`VACUUM INTO`，**不含密钥环**） |",
        f"| 权限 | 目录 {C.DIR_MODE:o} · 文件 {C.FILE_MODE:o} · umask {C.UMASK:o} |",
        "",
        "密钥环缺失**而库里已有密文** → **拒绝启动**。静默重新生成会让 setting 表的",
        "上游 key 永久解不开，且当时不报任何错。这是一扇单向门。",
        "",
        "上游 key 来源优先级："
        "`setting` 表（Fernet 加密）> `XINGCHA_OPENROUTER_API_KEY`（仅首次启动导入一次并告警）。",
        "",
        "## 11 · 计量",
        "",
        f"| 费用来源（`cost_source`） | {_fs(s.value for s in C.CostSource)} |",
        "|---|---|",
        f"| 输出保证档（`tier`） | {_fs(t.value for t in C.Tier)} |",
        f"| 判档依据 | 上游 catalog 的 `{C.CATALOG_NATIVE_SCHEMA_PARAM}`"
        "（**不能看 `response_format`**——两者不等价，混用会把 T2 误判成 T1） |",
        "",
        "四态与四档从第一天就写进数据库的 CHECK 约束，尽管 v1 只实现 T2 与前三种来源。",
        "没预留的话，补齐时就是一次需要重建表的迁移。",
        "",
    ]
    return "\n".join(out) + "\n"


DOC_PATH = Path(__file__).resolve().parents[2] / "docs" / "CONTRACT.md"


def write(path: Path | None = None) -> Path:
    target = path or DOC_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(), encoding="utf-8")
    return target


if __name__ == "__main__":
    p = write()
    print(f"已生成 {p}")
