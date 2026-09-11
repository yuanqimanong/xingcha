"""CLI 面。

------------------------------------------------------------------------------
为什么需要这一层
------------------------------------------------------------------------------

CLI 是**契约的一部分**（契约 §3.13 把它冻结成闭集），而它此前一条专门的测试
都没有。后果不是抽象的：

- ``xingcha agent apply`` 与 ``agent show`` 在闭集里写着，实际**不存在**；
- 而产品**主动指引用户去跑那条不存在的命令**——CLI 的空列表提示里写着
  「或 `xingcha agent apply <file.yaml>`」，导出 bundle 的 README 里也印着同一句。
  用户照做必然撞 `No such command`。
- 只有 export 没有 apply，等于导出是一扇**单向门**：能导出、不能导回来，
  而"低锁定"这个卖点恰恰要求两个方向都通。

改名或删命令不会让任何别的测试变红，所以闭集这个词此前没有任何执行机制。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import typer.main
from typer.testing import CliRunner

from xingcha import config as config_mod
from xingcha import contract as C
from xingcha.cli import app
from xingcha.db import migrate

#: §3.13 冻结的 CLI 闭集。改动它是契约变更，不是随手改名。
CLOSED_SET = {
    "serve": set(),
    "doctor": set(),
    "version": set(),
    "config": {"set", "get", "unset", "list"},
    "token": {"issue", "list", "revoke"},
    "agent": {"apply", "list", "show", "export"},
    "db": {"upgrade", "downgrade", "backup", "restore", "verify"},
    "quota": {"set", "list", "unset"},
    "admin": {"reset-password", "status"},
}

SCHEMA = {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]}


@pytest.fixture
def cli(tmp_path: Path):
    """一个初始化好库的 CLI runner。

    每个用例独立数据目录：CLI 命令走的是与 serve 完全相同的 bootstrap.prepare，
    共用目录会让密钥环与迁移状态串味。
    """
    data = tmp_path / "data"
    config_mod.reset_settings()
    runner = CliRunner(env={"XINGCHA_DATA_DIR": str(data)})
    settings = config_mod.Settings(data_dir=data)
    settings.ensure_data_dir()
    migrate.upgrade_to_head(settings.db_path, settings.backup_dir)
    try:
        yield runner
    finally:
        config_mod.reset_settings()


def run(cli: CliRunner, *args: str, stdin: str | None = None):
    return cli.invoke(app, list(args), input=stdin)


# =============================================================================
# 闭集
# =============================================================================


class TestClosedSet:
    def _subcommands(self, cli: CliRunner, group: str) -> set[str]:
        """某个命令组下**已注册**的子命令名。

        走 click 的命令树，**不解析 --help 的渲染结果**。曾经解析过，代价是 CI 里
        九个命令组一起"消失"：``GITHUB_ACTIONS`` 一存在，typer 就强制开颜色
        （``rich_utils.FORCE_TERMINAL``），命令名变成 ``\x1b[1;36mserve\x1b[0m``，
        逐行取首词的解析器把每一行都判成非字母数字而丢掉。本机复现不出来，因为
        非交互 shell 的 ``TERM=dumb`` 恰好让 typer 关掉颜色——**同一份代码，两台
        机器两种结果**，而红的那台看起来像"命令真的没了"。

        闭集要断言的是"命令注册了没有"，那是命令树里的事实；help 长什么样是渲染，
        受终端宽度、颜色、locale 影响。拿渲染结果去证注册事实，是把一条确定的断言
        建在一堆环境变量上。
        """
        root = typer.main.get_command(app)
        # 按 `.commands` 鸭子判定，**不用 isinstance(click.Group)**：typer 0.27 内置了
        # 自己的一份 click（``typer._click``），TyperGroup 的基类是
        # ``typer._click.core.Command`` 而不是 ``click.core.Group``——拿装在环境里的
        # click 去 isinstance 恒为假，而失败信息会说"顶层不是命令组"。
        commands = getattr(root, "commands", None)
        assert isinstance(commands, dict), "顶层命令树取不到（typer 的实现换了？）"
        if group == "--help":
            return set(commands)
        sub = getattr(commands.get(group), "commands", None)
        assert isinstance(sub, dict), f"`xingcha {group}` 这个命令组不存在"
        return set(sub)

    def test_every_promised_group_exists(self, cli: CliRunner):
        top = self._subcommands(cli, "--help")
        missing = set(CLOSED_SET) - top
        assert not missing, f"§3.13 承诺的命令组不存在：{sorted(missing)}"

    @pytest.mark.parametrize("group", sorted(k for k, v in CLOSED_SET.items() if v))
    def test_every_promised_subcommand_exists(self, cli: CliRunner, group: str):
        """闭集里列的子命令必须都在。

        `agent apply` 与 `agent show` 就是这样缺了很久的——而闭集没有测试，
        所以缺了也没人知道。
        """
        got = self._subcommands(cli, group)
        missing = CLOSED_SET[group] - got
        assert not missing, f"`xingcha {group}` 缺少：{sorted(missing)}"

    def test_no_undocumented_extras(self, cli: CliRunner):
        """反向也要守：多出来的命令说明闭集该更新了。

        闭集的意义是"改它要经过一次决定"，而不是"文档里恰好列了几条"。
        """
        top = self._subcommands(cli, "--help")
        extras = top - set(CLOSED_SET)
        assert not extras, f"这些命令不在 §3.13 闭集里：{sorted(extras)}"

    def test_no_command_advertises_a_command_that_does_not_exist(self, cli: CliRunner):
        """**产品不能指引用户去跑不存在的命令。**

        `agent list` 的空列表提示与导出 bundle 的 README 都写着
        `xingcha agent apply`，而它曾经不存在——用户照做必然撞 No such command。
        """
        from xingcha.contract import Tier
        from xingcha.core import exporter
        from xingcha.core.builder import Prompting

        readme = exporter._readme(
            slug="x",
            name="x",
            version=1,
            tier=Tier.T2,
            structured=True,
            has_runner=True,
            prompting=Prompting(),
        )
        advertised = set()
        for text in (run(cli, "agent", "list").output, readme):
            for group, subs in CLOSED_SET.items():
                for sub in subs:
                    if f"xingcha {group} {sub}" in text:
                        advertised.add((group, sub))
        assert advertised, "探针失效：一条被指引的命令都没找到"
        for group, sub in sorted(advertised):
            r = run(cli, group, sub, "--help")
            assert r.exit_code == 0, f"产品指引了 `xingcha {group} {sub}`，但它不存在"


# =============================================================================
# agent apply / show —— 导出的反向
# =============================================================================


class TestAgentApplyAndShow:
    def _bundle(self, tmp_path: Path, *, schema: bool = True) -> Path:
        """造一个 export 那样的目录形状。"""
        d = tmp_path / "extract"
        d.mkdir()
        (d / "agent.yaml").write_text(
            "model: openai/gpt-5\nname: 抽取\ninstructions: 抽取标题。\nretries: 2\n",
            encoding="utf-8",
        )
        if schema:
            (d / "schema.json").write_text(json.dumps(SCHEMA), encoding="utf-8")
        return d / "agent.yaml"

    def test_apply_infers_slug_and_schema_from_the_bundle_shape(
        self, cli: CliRunner, tmp_path: Path
    ):
        """对着 export 的产物直接 apply，不该需要任何选项。

        约定是 ``<dest>/<slug>/agent.yaml`` + 同目录 ``schema.json``——两边对齐，
        导出到应用之间就没有手工步骤。
        """
        r = run(cli, "agent", "apply", str(self._bundle(tmp_path)))
        assert r.exit_code == 0, r.output
        assert "extract" in r.output
        assert C.Tier.T2.value in r.output

        listed = run(cli, "agent", "list").output
        assert "extract" in listed and "结构化" in listed

    def test_export_edit_apply_round_trip_bumps_the_version(self, cli: CliRunner, tmp_path: Path):
        """**导出必须是双向门。**

        只有 export 没有 apply 的话，"低锁定"只兑现了一半：能拿走定义，
        拿不回来。
        """
        run(cli, "agent", "apply", str(self._bundle(tmp_path)))
        out = tmp_path / "out"
        assert run(cli, "agent", "export", "extract", "--dest", str(out)).exit_code == 0

        yaml_path = out / "extract" / "agent.yaml"
        yaml_path.write_text(
            yaml_path.read_text(encoding="utf-8").replace("抽取标题。", "改过的提示词。"),
            encoding="utf-8",
        )
        r = run(cli, "agent", "apply", str(yaml_path), "--changelog", "往返")
        assert r.exit_code == 0, r.output
        assert "v2" in r.output, "apply 应当产生新版本"

        shown = run(cli, "agent", "show", "extract").output
        assert "改过的提示词" in shown

    def test_show_then_apply_does_not_silently_drop_the_schema(
        self, cli: CliRunner, tmp_path: Path
    ):
        """``show`` 的输出里 schema 是**内嵌在 spec 的 output_schema 里**的。

        apply 若只认同目录的 schema.json，这条路径会把结构化 Agent **静默降成
        纯文本**——200 依旧、只是再也没有校验了，是最难发现的一种回归。
        """
        run(cli, "agent", "apply", str(self._bundle(tmp_path)))
        shown = run(cli, "agent", "show", "extract").stdout
        assert "output_schema" in shown

        path = tmp_path / "shown.yaml"
        path.write_text(shown, encoding="utf-8")
        r = run(cli, "agent", "apply", str(path), "--slug", "copy")
        assert r.exit_code == 0, r.output

        listed = run(cli, "agent", "list").output
        assert "copy" in listed
        # 关键断言：副本仍然是结构化的
        assert listed.count("结构化") == 2, f"schema 在 show→apply 之间丢了：\n{listed}"

    def test_show_stdout_is_clean_yaml(self, cli: CliRunner, tmp_path: Path):
        """人看的注释走 stderr，stdout 保持可直接管道给 apply。

        混在一起的话 `show | apply` 就废了，而那正是它最主要的用法。
        """
        import yaml

        run(cli, "agent", "apply", str(self._bundle(tmp_path)))
        spec = yaml.safe_load(run(cli, "agent", "show", "extract").stdout)
        assert isinstance(spec, dict)
        assert spec["model"] == "openai/gpt-5"

    def test_show_schema_only(self, cli: CliRunner, tmp_path: Path):
        run(cli, "agent", "apply", str(self._bundle(tmp_path)))
        got = json.loads(run(cli, "agent", "show", "extract", "--schema").stdout)
        assert got["required"] == ["title"]

    def test_apply_from_stdin_needs_an_explicit_slug(self, cli: CliRunner):
        """从 stdin 读时没有目录名可推断，必须显式给——不能猜一个出来。"""
        spec = "model: openai/gpt-5\nname: x\ninstructions: y\n"
        assert run(cli, "agent", "apply", "-", stdin=spec).exit_code != 0
        r = run(cli, "agent", "apply", "-", "--slug", "from-stdin", stdin=spec)
        assert r.exit_code == 0, r.output

    @pytest.mark.parametrize(
        ("spec", "expect"),
        [
            ("not: [a valid", "YAML"),
            ("name: 缺 model\n", "model"),
            ("model: openai/gpt-5\nnmae: 拼错了\n", "nmae"),
        ],
    )
    def test_bad_input_says_what_is_wrong(self, cli: CliRunner, spec: str, expect: str):
        """拼错的字段必须被官方 AgentSpec schema 挡住。

        AgentSpec 是 ``extra='ignore'``，直接构造会**静默吞掉**拼错的键——
        那时候你以为配了、其实跑的是默认值。
        """
        r = run(cli, "agent", "apply", "-", "--slug", "bad", stdin=spec)
        assert r.exit_code != 0
        assert expect in r.output

    def test_show_unknown_slug_is_a_clean_error(self, cli: CliRunner):
        r = run(cli, "agent", "show", "nope")
        assert r.exit_code != 0
        assert "Traceback" not in r.output, "运维类失败不该甩栈回溯"


# =============================================================================
# config set 的重启提示
# =============================================================================


class TestConfigSetTellsYouToRestart:
    """`config set` 写进库了、但**不重启不生效**（启动时读一次）。

    不提醒的话会出现最难受的一种失败：命令回了 ✓，服务照旧报"还没有配置
    OpenRouter API key"——而那句报错正好推荐了这条命令。用户会以为命令没生效、
    或者配置存错了地方，然后反复重试。
    """

    @pytest.mark.parametrize(
        "key",
        [
            C.SETTING_KEY_OPENROUTER_API_KEY,
            C.SETTING_KEY_OPENROUTER_BASE_URL,
            C.SETTING_KEY_TRACE_ENDPOINT,
        ],
    )
    def test_startup_only_keys_warn(self, cli: CliRunner, key: str):
        value = "sk-or-v1-x" if "api_key" in key else "https://example.com/v1"
        r = run(cli, "config", "set", key, value)
        assert r.exit_code == 0, r.output
        assert "重启" in r.output, f"{key} 改动后没有提示重启"

    def test_the_hint_names_the_ui_as_the_live_path(self, cli: CliRunner):
        """后台的设置页会当场重装（它调 load_upstream / load_tracing），要说清楚。"""
        r = run(cli, "config", "set", C.SETTING_KEY_OPENROUTER_API_KEY, "sk-or-v1-x")
        assert "设置" in r.output
