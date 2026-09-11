"""导出 bundle：把一个 Agent 变成可以脱离星槎运行的三个文件。

这是"低锁定"的唯一证明。**不是承诺，是可执行的验收**：干净虚拟环境里只装
``pydantic-ai-slim[openai,spec]`` 与 ``jsonschema``，用导出物跑通并**复现校验行为**
——验证方式是建一个干净 venv、只装 pydantic-ai，直接跑 ``run.py``。

产出：

.. code-block:: text

    extract/
    ├── agent.yaml     纯 AgentSpec，pydantic-ai 直接可读，零星槎依赖
    ├── schema.json    输出 JSON Schema
    ├── run.py         ~40 行校验 runner，只依赖 pydantic-ai + jsonschema
    └── README.md      如实写清保留了什么、丢失了什么

**为什么导出物与线上不是同一条代码路径**

线上：``builder`` 显式把 ``output_type=NativeOutput/ToolOutput(...)`` 传进
``from_spec``，档位是强制的。

导出物：``Agent.from_file`` **没有** ``output_type`` 注入点，只能靠 spec 里的
``output_schema`` 走 auto 模式。也就是说导出物永远是 auto，拿不到 T1 的
``strict=True`` 原生约束。

这不是偷懒，是上游 API 的边界。所以导出的 README 里必须如实写明——
把"档位强制"也列进"丢失"那一栏，而不是让人以为导出物和线上完全等价。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..contract import Tier

#: 独立 runner。**零 xingcha import** —— 这是"可退出"的字面含义。
#:
#: 校验逻辑抽成一个函数而不是内联进装饰器，是为了让它能被单独测试：
#: 干净环境里的验收要断言"违规数据真的被拦下"，而不是只断言"文件能 import"。
_RUN_HEAD = '''"""由星槎导出，可脱离星槎独立运行。

    pip install "pydantic-ai-slim[openai,spec]"{extra_deps}
    export OPENROUTER_API_KEY=sk-or-v1-...
    echo "你的输入" | python run.py

这个文件不 import 任何星槎的东西。删掉星槎，它照样跑。
"""

import json
import sys
from pathlib import Path

from pydantic_ai import Agent{retry_import}

HERE = Path(__file__).parent
'''

#: 校验块。只有带 schema 的 Agent 才写。
_RUN_VALIDATE = '''
import jsonschema

SCHEMA = json.loads((HERE / "schema.json").read_text(encoding="utf-8"))

# 只允许指向文档内部的 $ref。星槎在导出前已经把 $defs 内联展开了，所以正常情况下
# 这里根本不会遇到引用；空 registry 是防止有人事后手改 schema 引入远程引用——
# 那会让校验过程去访问那个地址。
try:
    from referencing import Registry

    _VALIDATOR = jsonschema.Draft202012Validator(SCHEMA, registry=Registry())
except ImportError:  # referencing 是 jsonschema 的依赖，正常都在
    _VALIDATOR = jsonschema.Draft202012Validator(SCHEMA)


def validate(data):
    """校验输出。不合规就抛 ModelRetry，让模型带着错误详情重来一次。

    这就是星槎在服务端做的事。没有它，声明式路径下的 schema 只是一句提示——
    模型输出什么都会被原样放行。
    """
    errors = sorted(_VALIDATOR.iter_errors(data), key=lambda e: list(e.absolute_path))
    if not errors:
        return data
    detail = "; ".join(
        f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}" for e in errors[:5]
    )
    raise ModelRetry(f"输出不符合 schema：{detail}")
'''

#: 提示词组装块。用户模板与少样本**不是** AgentSpec 的字段（官方 schema 里没有
#: 对应位置），星槎把它们存在 metadata 里、运行时自己应用。所以导出物不能只把
#: agent.yaml 带走了事——不烤进这个文件的话，导出的 Agent 收到的提示词和线上
#: 不是同一份，而这种差异只会在输出变差时才被察觉。
_RUN_PROMPTING = '''
# --- 提示词组装（星槎在服务端做的同一件事，这里是它的字面实现） ---

USER_TEMPLATE = {user_template!r}
EXAMPLES = {examples!r}


def frame(text):
    """把调用方的内容套进用户提示词模板。模板为空就原样返回。"""
    return USER_TEMPLATE.replace("{placeholder}", text) if USER_TEMPLATE else text


def few_shot():
    """少样本示例，以真正的对话轮送进去（不是拼进提示词里的一段文本）。"""
    from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

    history = []
    for pair in EXAMPLES:
        history.append(ModelRequest(parts=[UserPromptPart(content=frame(pair["user"]))]))
        history.append(ModelResponse(parts=[TextPart(content=pair["assistant"])]))
    return history
'''

#: 没有模板与示例时，两个函数仍然存在——省得 main 里到处 if。
_RUN_PROMPTING_NOOP = """

def frame(text):
    return text


def few_shot():
    return []
"""

_RUN_BUILD = '''

def build(model=None):
    """构造 Agent{and_validator}。``model`` 留空则用 agent.yaml 里写的那个。"""
    agent = Agent.from_file(HERE / "agent.yaml", **({{"model": model}} if model else {{}}))
{attach}    return agent


if __name__ == "__main__":
    text = sys.stdin.read()
    if not text.strip():
        print("从标准输入读取内容。用法：echo \\"...\\" | python run.py", file=sys.stderr)
        raise SystemExit(1)
    result = build().run_sync(frame(text), message_history=few_shot())
    output = result.output
    print(output if isinstance(output, str) else json.dumps(output, ensure_ascii=False, indent=2))
'''


def render_run_py(*, structured: bool, prompting: Any) -> str:
    """拼出独立 runner。**零 xingcha import** —— 这是"可退出"的字面含义。

    按需拼装而不是一个大模板里插 if：导出物是给人读的，一个满是"如果没有 schema
    就忽略下面这段"的文件，读者没法确定自己这份到底走哪条路。
    """
    from .builder import PROMPT_PLACEHOLDER

    head = _RUN_HEAD.format(
        extra_deps=" jsonschema" if structured else "",
        retry_import=", ModelRetry" if structured else "",
    )
    body = _RUN_VALIDATE if structured else ""
    if prompting is not None and not prompting.is_empty:
        body += _RUN_PROMPTING.format(
            user_template=prompting.user_template,
            examples=[{"user": e.user, "assistant": e.assistant} for e in prompting.examples],
            placeholder=PROMPT_PLACEHOLDER,
        )
    else:
        body += _RUN_PROMPTING_NOOP
    tail = _RUN_BUILD.format(
        and_validator=" 并挂上校验" if structured else "",
        attach="    agent.output_validator(validate)\n" if structured else "",
    )
    return head + body + tail


@dataclass(frozen=True, slots=True)
class Bundle:
    directory: Path
    files: tuple[str, ...]


def export(
    *,
    slug: str,
    name: str,
    version: int,
    tier: Tier,
    spec: dict[str, Any],
    out_schema: dict[str, Any] | None,
    dest: Path,
) -> Bundle:
    """写出 bundle。返回目录与文件清单。"""
    directory = dest / slug
    directory.mkdir(parents=True, exist_ok=True)

    spec = dict(spec)
    # schema 必须留在 spec 里：导出物靠 from_file → output_schema 拿到结构化输出，
    # 那是它唯一的注入点。线上则是显式传 output_type，两条路径不冲突。
    if out_schema is not None:
        spec["output_schema"] = out_schema

    files = ["agent.yaml", "README.md"]
    (directory / "agent.yaml").write_text(
        yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    # run.py 的触发条件不只是"有 schema"。
    #
    # 用户提示词模板与少样本存在 AgentSpec.metadata 里，而**上游不解释 metadata**：
    # 光把 agent.yaml 带走的话，导出的 Agent 收到的提示词跟线上不是同一份，而这种
    # 差异只在输出变差时才被察觉。所以只要有其中之一，就得给出一个能跑对的 runner。
    from .builder import prompting_from_spec

    prompting = prompting_from_spec(spec)
    if out_schema is not None:
        (directory / "schema.json").write_text(
            json.dumps(out_schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        files.insert(1, "schema.json")
    if out_schema is not None or not prompting.is_empty:
        (directory / "run.py").write_text(
            render_run_py(structured=out_schema is not None, prompting=prompting), encoding="utf-8"
        )
        files.insert(-1, "run.py")

    (directory / "README.md").write_text(
        _readme(
            slug=slug,
            name=name,
            version=version,
            tier=tier,
            structured=out_schema is not None,
            has_runner="run.py" in files,
            prompting=prompting,
        ),
        encoding="utf-8",
    )
    return Bundle(directory=directory, files=tuple(files))


def _readme(
    *,
    slug: str,
    name: str,
    version: int,
    tier: Tier,
    structured: bool,
    has_runner: bool,
    prompting: Any,
) -> str:
    """随导出物附一份**诚实**的说明。

    "保留 / 丢失"那张表是这份文档的核心：一个只写好处的导出说明会让人在真正需要
    迁移的那天才发现少了东西，而那时已经来不及了。
    """
    if not structured:
        run_block = (
            (
                "```bash\n"
                'pip install "pydantic-ai-slim[openai,spec]"\n'
                "export OPENROUTER_API_KEY=sk-or-v1-...\n"
                'echo "你的输入" | python run.py\n'
                "```\n"
            )
            if has_runner
            else (
                "```python\n"
                "from pydantic_ai import Agent\n\n"
                'agent = Agent.from_file("agent.yaml")   # 零星槎依赖\n'
                'print(agent.run_sync("你的输入").output)\n'
                "```\n"
            )
        )
        kept = "模型 · 指令 · 内置能力 · 重试 · 模型参数"
        if has_runner:
            kept += " · **用户模板与少样本**（经 `run.py`）"
        lost = "用量计量 · 配额 · 版本历史"
        extra = ""
    else:
        run_block = (
            "```bash\n"
            'pip install "pydantic-ai-slim[openai,spec]" jsonschema\n'
            "export OPENROUTER_API_KEY=sk-or-v1-...\n"
            'echo "你的输入" | python run.py\n'
            "```\n"
        )
        kept = (
            "模型 · 指令 · 内置能力 · 重试 · 模型参数 · schema（作模型指令）· "
            "**运行时校验与重试**（经 `run.py`）"
        )
        if not prompting.is_empty:
            kept += " · **用户模板与少样本**"
        lost = "用量计量 · 配额 · 版本历史 · 自动判档 · **档位强制**"
        extra = f"""
## 一处必须说清楚的差异

星槎线上把档位**强制**施加给模型（`output_type=NativeOutput(...)` 之类）。
导出物做不到这件事：`Agent.from_file` 没有 `output_type` 注入点，只能靠
`agent.yaml` 里的 `output_schema` 走 auto 模式。

对这个 Agent（档位 **{tier.value}**）来说，具体意味着：

- **校验与重试完全保留**——`run.py` 里的 `validate()` 就是星槎服务端做的事，
  违规输出会带着错误详情打回模型重来。
- **拿不到 `strict=True` 的原生约束解码**。如果原本是 T1 / T1+ 档，导出后的形状
  保证会降到与 T2 相当：靠校验重试，而不是靠上游的解码约束。

换句话说：**内容保证一样强，形状保证可能弱一档**。
"""

    runner_desc = (
        " + ".join(
            x
            for x in (
                "挂上运行时校验" if structured else "",
                "套用用户模板与少样本" if not prompting.is_empty else "",
            )
            if x
        )
        + " 的 runner。零星槎 import"
    )

    return f"""# {name}

由星槎导出 —— Agent `{slug}`，第 {version} 版，保证档位 **{tier.value}**。

这个目录**不依赖星槎**。删掉星槎，它照样跑。

## 运行

{run_block}
## 文件

| 文件 | 内容 |
|---|---|
| `agent.yaml` | 纯 `AgentSpec`，`pydantic_ai.Agent.from_file()` 直接可读 |
{"| `schema.json` | 输出 JSON Schema（`$defs` 已内联展开） |" if structured else ""}
{f"| `run.py` | {runner_desc} |" if has_runner else ""}

## 导出后保留 / 丢失

| 保留 | 丢失 |
|---|---|
| {kept} | {lost} |
{extra}
## 改回来

这个目录里的 `agent.yaml` 可以直接贴回星槎的 Agent 表单（或
`xingcha agent apply agent.yaml`），因为它就是标准的 `AgentSpec`，不是星槎的私有格式。
"""
