"""导出 bundle。

**这是「低锁定」的唯一证明。**

其它测试可以在项目的 venv 里跑，这一组不行：整个卖点是"导出物不依赖星槎"，
而在装了星槎的环境里跑，无论如何都证明不了这件事。所以 :class:`TestCleanEnvironment`
真的建一个干净虚拟环境、只装 README 里承诺的那两个依赖，然后在里面跑导出物。

那组测试慢（要建 venv 装包），标了 ``slow``，默认仍然跑——它证明的东西值这个时间。
不想等时用 ``pytest -m "not slow"``。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from xingcha.contract import Tier
from xingcha.core import exporter
from xingcha.core.schema_guard import validate_schema

SCHEMA = validate_schema(
    {
        "type": "object",
        "properties": {
            "客户名称": {"type": "string", "description": "合同甲方的完整名称"},
            "金额": {"type": "number"},
        },
        "required": ["客户名称", "金额"],
    }
)

SPEC = {
    "model": "openai/gpt-5",
    "name": "合同抽取",
    "instructions": "从合同文本里抽取甲方名称与金额。",
    "retries": 2,
}


@pytest.fixture
def bundle(tmp_path: Path) -> exporter.Bundle:
    return exporter.export(
        slug="extract",
        name="合同抽取",
        version=3,
        tier=Tier.T2,
        spec=SPEC,
        out_schema=SCHEMA,
        dest=tmp_path,
    )


# =============================================================================
# 产出物
# =============================================================================


class TestBundleShape:
    def test_three_files_plus_readme(self, bundle: exporter.Bundle):
        assert set(bundle.files) == {"agent.yaml", "schema.json", "run.py", "README.md"}
        for f in bundle.files:
            assert (bundle.directory / f).exists()

    def test_agent_yaml_is_pure_agentspec(self, bundle: exporter.Bundle):
        """**不是星槎的私有格式。**

        这是"抄不走"的那部分之所以成立的前提：竞品的配置都是自有 schema，
        要达到同等可移植性等于把自己的核心重写在别人的框架上。
        """
        from pydantic_ai import AgentSpec

        spec = yaml.safe_load((bundle.directory / "agent.yaml").read_text(encoding="utf-8"))
        parsed = AgentSpec.model_validate(spec)  # 官方模型直接吃下去
        assert parsed.model == "openai/gpt-5"
        assert parsed.output_schema == SCHEMA

    def test_yaml_has_no_xingcha_fields(self, bundle: exporter.Bundle):
        """spec 里混进自有字段就不再是纯 AgentSpec 了。"""
        raw = (bundle.directory / "agent.yaml").read_text(encoding="utf-8")
        for word in ("xingcha", "tier", "slug", "x_xingcha"):
            assert word not in raw.lower()

    def test_run_py_imports_nothing_from_xingcha(self, bundle: exporter.Bundle):
        """ "可退出"的字面含义：删掉星槎，它照样跑。"""
        raw = (bundle.directory / "run.py").read_text(encoding="utf-8")
        assert "xingcha" not in raw

    def test_schema_is_the_inlined_one(self, bundle: exporter.Bundle):
        """导出的是内联展开后的 schema。

        带 $ref 的原文会让 run.py 的校验器与模型收到的约束不是同一份。
        """
        written = json.loads((bundle.directory / "schema.json").read_text(encoding="utf-8"))
        assert "$ref" not in json.dumps(written)
        assert written == SCHEMA

    def test_plain_text_agent_exports_two_files(self, tmp_path: Path):
        """纯文本 Agent 没有 schema，也就不需要 run.py 与 schema.json。"""
        b = exporter.export(
            slug="chat",
            name="闲聊",
            version=1,
            tier=Tier.T3,
            spec={"model": "openai/gpt-5", "instructions": "聊天"},
            out_schema=None,
            dest=tmp_path,
        )
        assert set(b.files) == {"agent.yaml", "README.md"}


class TestHonestReadme:
    """ "保留 / 丢失"那张表是这份文档的核心。

    一个只写好处的导出说明，会让人在真正需要迁移的那天才发现少了东西。
    """

    def test_lists_what_is_lost(self, bundle: exporter.Bundle):
        readme = (bundle.directory / "README.md").read_text(encoding="utf-8")
        for lost in ("用量计量", "配额", "版本历史", "档位强制"):
            assert lost in readme

    def test_explains_the_tier_gap(self, bundle: exporter.Bundle):
        """导出物拿不到 strict=True 的原生约束——这是上游 API 的边界，必须写明。"""
        readme = (bundle.directory / "README.md").read_text(encoding="utf-8")
        assert "from_file" in readme
        assert "output_type" in readme
        assert "形状保证可能弱一档" in readme

    def test_says_validation_is_kept(self, bundle: exporter.Bundle):
        readme = (bundle.directory / "README.md").read_text(encoding="utf-8")
        assert "运行时校验" in readme or "校验与重试完全保留" in readme

    def test_tells_you_how_to_come_back(self, bundle: exporter.Bundle):
        """低锁定是双向的：走得掉，也回得来。"""
        readme = (bundle.directory / "README.md").read_text(encoding="utf-8")
        assert "改回来" in readme


# =============================================================================
# 干净环境 —— 这一组才是真正的验收
# =============================================================================

CLEAN_DRIVER = '''
"""在干净环境里驱动导出物。只用 FunctionModel，不需要任何 API key。"""
import importlib.util, json, sys
from pathlib import Path

# 先证明星槎确实不在这个环境里
assert importlib.util.find_spec("xingcha") is None, "干净环境里不该有 xingcha"

bundle = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("exported_run", bundle / "run.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)          # 只有 pydantic-ai + jsonschema 时能 import

from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

BAD = {"金额": "不是数字"}            # 缺必填 + 类型错
GOOD = {"客户名称": "某某公司", "金额": 1234.5}

def model_returning(payloads):
    calls = {"n": 0}
    def fn(messages, info):
        i = calls["n"]; calls["n"] += 1
        body = payloads[min(i, len(payloads) - 1)]
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, body)])
    return FunctionModel(fn), calls

results = {}

# 1 违规必须被拦下并重试到耗尽
m, calls = model_returning([BAD])
try:
    mod.build(model=m).run_sync("合同文本")
    results["blocks_violations"] = False
except UnexpectedModelBehavior:
    results["blocks_violations"] = True
results["calls_on_exhaustion"] = calls["n"]

# 2 先违规后修正必须自动恢复
m, calls = model_returning([BAD, GOOD])
out = mod.build(model=m).run_sync("合同文本").output
results["recovers"] = out == GOOD
results["calls_on_recovery"] = calls["n"]

# 3 合规数据一次通过
m, calls = model_returning([GOOD])
results["passes_valid"] = mod.build(model=m).run_sync("合同文本").output == GOOD
results["calls_on_valid"] = calls["n"]

# 4 validate() 单独可用，且错误消息说清了是哪个字段
try:
    mod.validate(BAD)
    results["validate_raises"] = False
except Exception as e:
    results["validate_raises"] = True
    results["validate_message"] = str(e)

print(json.dumps(results, ensure_ascii=False))
'''


@pytest.mark.slow
class TestCleanEnvironment:
    """在只装了 README 承诺的两个依赖的干净 venv 里跑导出物。

    这是唯一能证明"低锁定"的方式。在项目自己的 venv 里跑，无论断言什么，
    都可能是因为星槎恰好也在那儿。
    """

    @staticmethod
    @pytest.fixture(scope="class")
    def clean_run(tmp_path_factory: pytest.TempPathFactory) -> dict:
        import shutil

        if shutil.which("uv") is None:  # pragma: no cover
            pytest.skip("需要 uv 来建干净环境")

        root = tmp_path_factory.mktemp("clean")
        bundle = exporter.export(
            slug="extract",
            name="合同抽取",
            version=3,
            tier=Tier.T2,
            spec=SPEC,
            out_schema=SCHEMA,
            dest=root,
        )

        venv = root / ".venv"
        subprocess.run(
            ["uv", "venv", "--python", "3.13", str(venv)],
            check=True,
            capture_output=True,
            timeout=180,
        )
        # **只装 README 里承诺的这两个**。多装一个都不算数。
        subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(venv / "bin" / "python"),
                "pydantic-ai-slim[openai,spec]",
                "jsonschema",
            ],
            check=True,
            capture_output=True,
            timeout=600,
        )

        driver = root / "drive.py"
        driver.write_text(CLEAN_DRIVER, encoding="utf-8")
        proc = subprocess.run(
            [str(venv / "bin" / "python"), str(driver), str(bundle.directory)],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if proc.returncode != 0:  # pragma: no cover
            pytest.fail(f"干净环境里跑失败：\n{proc.stdout}\n{proc.stderr}")
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_runs_without_xingcha_installed(self, clean_run: dict):
        """驱动脚本自己先断言了 xingcha 不在环境里；能跑到这里就说明确实不在。"""
        assert clean_run

    def test_reproduces_validation(self, clean_run: dict):
        """**这条才是关键。**

        导出物"能 import"是不够的——那只证明文件语法对。要证明的是运行时校验行为
        跟着一起走了：违规数据在离开星槎之后照样被拦下。
        """
        assert clean_run["blocks_violations"] is True
        assert clean_run["calls_on_exhaustion"] == 3, "retries=2 → 1+2 次调用"

    def test_reproduces_retry_recovery(self, clean_run: dict):
        """重试的价值不只是"失败得干净"，而是"模型改对了就能救回来"。"""
        assert clean_run["recovers"] is True
        assert clean_run["calls_on_recovery"] == 2

    def test_valid_output_passes_in_one_call(self, clean_run: dict):
        assert clean_run["passes_valid"] is True
        assert clean_run["calls_on_valid"] == 1

    def test_error_message_names_the_field(self, clean_run: dict):
        """离开星槎之后，报错也得能定位。"""
        assert clean_run["validate_raises"] is True
        assert "客户名称" in clean_run["validate_message"]
