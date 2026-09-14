"""删除 Agent：必须先停用，且只清该清的。

星槎原本**刻意没有删除**——见 ``services.agent.set_active`` 一带的注释：调用方代码里
写着这个 slug，删掉之后它们收到 ``model_not_found``，而且**这个 slug 会重新变得可被
占用**，下一个同名 Agent 会悄悄接管那些调用。所以删除被拆成两下：停用（可逆）→ 确认
→ 删除，任何一下单独发生都不该生效。

这里守三件事：

1. 启用中的 Agent 删不掉——**服务层拦，不是靠页面不摆按钮**。页面那层是体验，
   服务层那层才是约束（直接发 POST 的人绕不过去）。
2. 连带清 ``agent_test_run`` 与 ``quota``：两者都没有外键，不手工清的话，下一个占用
   同名 slug 的 Agent 会继承别人的试运行记录；而 SQLite 复用 rowid，一条留下来的配额
   可能静默套到将来某个毫不相干的 Agent 头上。
3. **保留 ``run``**：调用记录与账单是已经发生的事实，不该因为清理一个 Agent 而消失。

DB 用 tmp_path 起一份干净的，跑迁移，不碰网络。同步写法——CLI 与服务层这条路上自带
``asyncio.run()``，塞进 pytest-asyncio 的事件循环里会直接 RuntimeError。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest

from xingcha.config import get_settings, reset_settings


@pytest.fixture
def db_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("XINGCHA_DATA_DIR", str(tmp_path / "data"))
    # 本机 .env 里的真 key 不能混进来：有 key 就会去拉模型目录（走网络）。
    for name in ("XINGCHA_API_KEY", "XINGCHA_OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    reset_settings()
    yield
    reset_settings()


def _run(coro_factory):
    """建库 → 跑迁移 → 在一个会话里执行 ``coro_factory(session)``。"""
    from xingcha.db.engine import make_engine, make_sessionmaker, session_scope
    from xingcha.db.migrate import upgrade_to_head

    async def go():
        settings = get_settings()
        settings.ensure_data_dir()
        # 建目录与迁移都是同步的，在这条同步测试里直接调。
        upgrade_to_head(settings.db_path)
        engine = make_engine(settings.db_path)
        try:
            async with session_scope(make_sessionmaker(engine)) as s:
                return await coro_factory(s)
        finally:
            await engine.dispose()

    return asyncio.run(go())


async def _seed(s, *, active: bool):
    """一个 Agent + 一条版本 / 试运行 / 配额 / 调用记录。"""
    from xingcha.db.models import Agent, AgentTestRun, AgentVersion, Quota, Run

    agent = Agent(slug="doomed", name="doomed", is_active=active)
    s.add(agent)
    await s.flush()
    s.add(AgentVersion(agent_id=agent.id, version=1, spec_json="{}", tier="none"))
    s.add(AgentTestRun(slug="doomed", model="m", tier="none", ok=True, input="i", output="o"))
    s.add(Quota(subject_type="agent", subject_id=agent.id, window="day", limit_usd="1.0"))
    s.add(
        Run(id="r1", kind="agent", agent_id=agent.id, agent_version=1, model="doomed", status="ok")
    )
    await s.flush()
    return agent.id


async def _counts(s, agent_id: int) -> dict[str, int]:
    from sqlalchemy import func, select

    from xingcha.db.models import Agent, AgentTestRun, AgentVersion, Quota, Run

    async def n(stmt) -> int:
        return int((await s.execute(stmt)).scalar_one())

    return {
        "agent": await n(select(func.count()).select_from(Agent).where(Agent.slug == "doomed")),
        "version": await n(
            select(func.count()).select_from(AgentVersion).where(AgentVersion.agent_id == agent_id)
        ),
        "test_run": await n(
            select(func.count()).select_from(AgentTestRun).where(AgentTestRun.slug == "doomed")
        ),
        "quota": await n(
            select(func.count())
            .select_from(Quota)
            .where(Quota.subject_type == "agent", Quota.subject_id == agent_id)
        ),
        "run": await n(select(func.count()).select_from(Run).where(Run.id == "r1")),
    }


def test_active_agent_cannot_be_deleted(db_home: None) -> None:
    """**服务层**拦住，不是靠页面不摆按钮——直接发 POST 的人绕不过去。"""
    from xingcha.services import agent as agent_svc

    async def go(s):
        agent_id = await _seed(s, active=True)
        with pytest.raises(agent_svc.AgentStillActive):
            await agent_svc.delete(s, "doomed")
        return await _counts(s, agent_id)

    assert _run(go)["agent"] == 1, "启用中的 Agent 被删掉了"


def test_deleting_a_disabled_agent_keeps_the_call_records(db_home: None) -> None:
    from xingcha.services import agent as agent_svc

    async def go(s):
        agent_id = await _seed(s, active=False)
        await agent_svc.delete(s, "doomed")
        await s.flush()
        return await _counts(s, agent_id)

    after = _run(go)
    assert after["agent"] == 0
    assert after["version"] == 0, "版本没跟着删（外键 CASCADE 没生效？）"
    assert after["test_run"] == 0, "试运行没清——下一个同名 Agent 会继承它们"
    assert after["quota"] == 0, "配额没清——SQLite 复用 rowid，它会套到别的 Agent 头上"
    assert after["run"] == 1, "调用记录被删了——账单是已经发生的事实，不该消失"


def test_deleting_a_missing_agent_raises(db_home: None) -> None:
    from xingcha.foundation.errors import ModelNotFound
    from xingcha.services import agent as agent_svc

    async def go(s):
        with pytest.raises(ModelNotFound):
            await agent_svc.delete(s, "never-existed")

    _run(go)


def test_button_only_renders_when_disabled() -> None:
    """页面那一层：启用状态下**连按钮都不摆**。

    摆一个点了必然被拒的开关比不摆更糟——它把"这件事现在做不了"变成"这件事好像
    坏了"。静态检查，不起服务。
    """
    src = Path(__file__).resolve().parents[1] / "src" / "xingcha"
    html = (src / "web" / "templates" / "agent_form.html").read_text(encoding="utf-8")

    assert "/delete" in html, "详情页上没有删除入口了？"
    before = html.split("/delete")[0]
    # 删除表单必须落在「未启用」那个分支里。取它前面最近的一个 {% if %}。
    assert "{% if not is_active %}" in before, "删除按钮不在 not is_active 分支里"
    assert before.rindex("{% if not is_active %}") > before.rindex("{% endif %}"), (
        "删除按钮所在的 if 分支在它之前就闭合了——启用状态下也会渲染出来"
    )
    assert "data-confirm" in html.split("/delete")[1][:400], "删除没有二次确认"
