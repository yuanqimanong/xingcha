"""让 ``python -m xingcha.cli`` 和 ``xingcha`` 走同一条路。

装了包就该用 ``xingcha``（``pyproject.toml`` 的 console script）。这个入口是给
「装不上 console script 但能跑 python」的场合留的——容器里排障时用得到。
"""

from __future__ import annotations

from . import app

app()
