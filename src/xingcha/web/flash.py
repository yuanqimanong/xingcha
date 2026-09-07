"""一次性展示值。

------------------------------------------------------------------------------
它替掉了什么
------------------------------------------------------------------------------

签发密钥之后要把明文显示一次。原先的做法是 ``303 → /admin/keys?issued=sk-xc-...``，
注释里写着"这不理想（会进浏览器历史），但替代方案（存进服务端 flash）会让明文在库里
多活一会儿——两害相权"。

**那是个假两难：服务端 flash 不必进库。** 单 worker 是断言过的硬约束
（``contract.REQUIRED_WORKERS == 1``，启动时校验），所以进程内存里做一次性存取就够了。
于是 URL 方案的每一条代价都消失：

- 不进浏览器历史，不留在地址栏（截图、录屏、肩窥都不再泄漏）；
- 不进 ``Referer``（同源策略只是缩小了范围，没有消除）；
- **刷新不再重现**——"这是唯一一次看到明文"这句话由此才成立；
- 明文一次都不落盘。

而它换来的成本只是"进程重启后取不到"——那一刻用户本来也已经看过或没看过了，
重启丢掉一个待展示的明文没有任何后果。
"""

from __future__ import annotations

import time
from collections import OrderedDict

#: 存活时长。够用户从 303 走完一次 GET，不够任何别的用途。
TTL_SECONDS = 60.0

#: 上界。它是"写进来等着被取走"的缓存，而取走那一步可能不发生（用户关掉了页面）。
#: 无界的话会慢慢吃内存，而这台机器只有 1GB。
MAX_ENTRIES = 32


class OneShotFlash:
    """按会话存一个待展示的值，**取走即删**。

    不是通用的 flash 框架：它只服务"显示一次就永久消失"这一种需求，所以没有
    多键、没有类型、没有持久化。多加一层通用性就会有人拿它存别的东西，
    而这里存的是明文密钥。
    """

    __slots__ = ("_items",)

    def __init__(self) -> None:
        self._items: OrderedDict[str, tuple[float, str]] = OrderedDict()

    def put(self, key: str, value: str) -> None:
        """``key`` 由调用方拼成 ``f"{session_id}:{用途}"``。

        按用途分槽而不是一个会话只有一格：签发密钥与保存 Agent 是两件事，
        共用一格会出现"存了后者、前者被顶掉"的串味。
        """
        if not key:
            return
        self._prune()
        self._items[key] = (time.monotonic() + TTL_SECONDS, value)
        self._items.move_to_end(key)
        while len(self._items) > MAX_ENTRIES:
            self._items.popitem(last=False)

    def take(self, key: str) -> str | None:
        """取走并删除。过期的当作不存在。"""
        self._prune()
        item = self._items.pop(key, None)
        if item is None:
            return None
        expires, value = item
        return value if expires > time.monotonic() else None

    def _prune(self) -> None:
        now = time.monotonic()
        for k in [k for k, (exp, _) in self._items.items() if exp <= now]:
            self._items.pop(k, None)

    def __len__(self) -> int:
        self._prune()
        return len(self._items)
