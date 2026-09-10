"""标识生成。

Agent 的 slug 是**对外的 ``model`` 值**，调用方代码里写着它，发布后不能改。所以
新建时给的默认值必须一次到位：不会撞、不会因为"两个人同一秒建"而重复。

用 UUIDv7 而不是随机 UUIDv4：v7 的高 48 位是毫秒时间戳，所以字典序 == 创建顺序。
后台按 slug 排序时新建的自然排在一起，而 v4 排出来是一堆噪声。
"""

from __future__ import annotations

import os
import time

#: slug 的正则是 ``[a-z][a-z0-9]*(-[a-z0-9]+)*``——**必须字母开头**，而 uuid 的
#: 十六进制半数时候是数字。前缀一个 ``a`` 是最省事的合规办法，同时也让 slug 一眼
#: 看得出是"自动生成的、还没起名字的那个"。
SLUG_PREFIX = "a"


def uuid7_hex() -> str:
    """一个 UUIDv7 的 32 位十六进制（不带连字符）。

    Python 3.14 才有 ``uuid.uuid7``，这里自己拼——只有 12 行，比为它抬一个依赖划算。
    布局照 RFC 9562：48 位毫秒 + 4 位版本 + 12 位随机 + 2 位变体 + 62 位随机。
    """
    ms = int(time.time() * 1000) & 0xFFFF_FFFF_FFFF
    rand_a = int.from_bytes(os.urandom(2), "big") & 0x0FFF
    rand_b = int.from_bytes(os.urandom(8), "big") & 0x3FFF_FFFF_FFFF_FFFF
    value = (ms << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    return f"{value:032x}"


#: 默认 slug 里保留几位随机。
#:
#: 完整的 uuid7 是 32 位十六进制，加前缀 33 个字符——**那是个没人会留着的默认值**：
#: 它是对外的 ``model`` 字符串，要出现在业务代码里、出现在卡片标题上、出现在错误
#: 信息里。所以这里只留"够用"的部分：12 位毫秒时间戳（有序性来自它）+ 6 位随机，
#: 连前缀与连字符共 20 个字符。
#:
#: 6 位 = 24 bit，同一毫秒内要撞上得同时建 4000 个左右，而这是单管理员的后台；
#: 真撞了也只是保存时报"标识已被占用"，不会静默出错（``slug_available`` 挡着）。
SLUG_RANDOM_HEX = 6


def new_agent_slug() -> str:
    """新建 Agent 时填进「标识」的默认值。用户可以整个改掉。"""
    h = uuid7_hex()
    return f"{SLUG_PREFIX}{h[:12]}-{h[-SLUG_RANDOM_HEX:]}"
