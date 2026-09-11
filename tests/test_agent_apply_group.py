"""``xingcha agent apply`` 不许动 Agent 的分组。

回归测试。此前 ``apply`` 从不传 ``group_name``，而 ``save()`` 把分组当**表单里的
一项**处理（不传 = 挪回默认组）——那对后台的表单是对的，但命令行里"这次没提这一项"
不等于"把它挪走"。于是每次 ``apply`` 都会静默把 Agent 踢回默认组，而 ``apply``
的输出一个字都不提分组。

走真正的 CLI（``CliRunner`` 调 typer app），不是直接调服务层：bug 就在 CLI 与服务层
之间的那一次参数传递上，只测服务层的话它是绿的。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from xingcha.cli import app
from xingcha.config import reset_settings

AGENT_YAML = """\
name: extract
model: openai/gpt-5
instructions: 把输入抽成结构化数据。
"""

runner = CliRunner()


@pytest.fixture
def cli_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """一个干净的数据目录 + 一份 agent.yaml。CLI 自己会建库跑迁移。"""
    monkeypatch.setenv("XINGCHA_DATA_DIR", str(tmp_path / "data"))
    # 别让本机 .env 里的真 key 混进来：有 key 的话 apply 会去拉模型目录（走网络）。
    for name in ("XINGCHA_API_KEY", "XINGCHA_OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    reset_settings()

    spec_dir = tmp_path / "agents" / "extract"
    spec_dir.mkdir(parents=True)
    (spec_dir / "agent.yaml").write_text(AGENT_YAML, encoding="utf-8")

    yield spec_dir

    reset_settings()


def _apply(spec_dir: Path, *extra: str) -> None:
    result = runner.invoke(app, ["agent", "apply", str(spec_dir / "agent.yaml"), *extra])
    assert result.exit_code == 0, result.output


def _group_of(slug: str) -> str | None:
    """直接读库，绕开 CLI 的输出——它本来就不打印分组，那正是 bug 难发现的原因。

    这几条测试**必须是同步的**：CLI 自己用 ``asyncio.run()``，跑在 pytest-asyncio
    的事件循环里会直接 RuntimeError。所以读库这一步也自带 ``asyncio.run``。
    """
    from xingcha.config import get_settings
    from xingcha.db.engine import make_engine, make_sessionmaker, session_scope
    from xingcha.services import agent as agent_svc

    async def go() -> str | None:
        engine = make_engine(get_settings().db_path)
        try:
            async with session_scope(make_sessionmaker(engine)) as s:
                return await agent_svc.current_group(s, slug)
        finally:
            await engine.dispose()

    return asyncio.run(go())


def test_apply_without_group_keeps_the_existing_one(cli_home: Path):
    _apply(cli_home, "--group", "生产线")
    assert _group_of("extract") == "生产线"

    # 改了提示词再 apply 一遍，没提分组——分组必须还在。
    (cli_home / "agent.yaml").write_text(
        AGENT_YAML.replace("把输入抽成结构化数据。", "改了一版。"), encoding="utf-8"
    )
    _apply(cli_home)
    assert _group_of("extract") == "生产线", "apply 把 Agent 踢出了它的分组"


def test_apply_can_still_move_between_groups(cli_home: Path):
    _apply(cli_home, "--group", "生产线")
    _apply(cli_home, "--group", "实验")
    assert _group_of("extract") == "实验"


def test_empty_group_moves_back_to_default(cli_home: Path):
    """``--group ""`` 是"挪回默认组"的显式写法。

    保留一条显式通道是必要的：否则 CLI 建的 Agent 一旦进了某个组就再也出不来。
    """
    _apply(cli_home, "--group", "生产线")
    _apply(cli_home, "--group", "")
    assert _group_of("extract") is None


def test_new_agent_without_group_lands_in_default(cli_home: Path):
    _apply(cli_home)
    assert _group_of("extract") is None
