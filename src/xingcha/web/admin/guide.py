"""调用指南。

一页把「拿到 key → 配好 Agent → 用代码调它」讲完。

单独一页而不是散在各页的提示里：这些步骤**跨页**（密钥页签 key、Agent 页配提示词、
然后回到自己的代码里），而散着写的后果是每一页都只说自己那一段，没有任何地方能
从头读到尾。给新来的人发一个链接就够了。

内容是静态的，只有 ``public_url`` 与示例里的模型名取自运行时——写死 localhost 的话
用户复制那条 curl 拿到别的机器上必然连不上，而错误信息指不到"地址是抄来的"。
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from ...services import agent as agent_svc
from .render import page
from .security import require_admin

router = APIRouter(prefix="/admin", include_in_schema=False)


@router.get("/guide")
async def guide_page(request: Request) -> Response:
    await require_admin(request)
    state = request.app.state.xc

    # 拿一个真实存在的 Agent 名字放进示例。没有 Agent 时退回占位符——
    # 示例里印一个不存在的 model，照着跑会拿到 404，而那一页正是用来"照着跑"的。
    async with state.sessionmaker() as s:
        pairs = await agent_svc.list_all(s)
    sample = next((row.slug for row, ver in pairs if row.is_active and ver), "你的-agent-标识")

    return await page(
        request,
        "guide.html",
        {
            "sample_agent": sample,
            "has_agent": bool(pairs),
            # 直通示例里的裸模型名取自真实目录：写死 openai/gpt-5 而当前上游是
            # DeepSeek 的话，照着跑会直接报 model_not_found。
            "sample_model": (state.catalog.all()[0].id if state.catalog.all() else "openai/gpt-5"),
        },
    )
