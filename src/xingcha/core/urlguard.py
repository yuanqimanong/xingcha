"""上游地址的校验。

管理员能在设置页填写"中转地址"，而那个地址会被拿去发带着 OpenRouter key 的请求。
这意味着它是一个 **SSRF 原语**：不加约束的话，一个被 CSRF 改写的 base_url 就能把
付费 key 送到任意主机，而这台机器还会顺便变成一个能打内网的通用代理。

三道约束，缺一不可：

1. **只允许 https**（例外仅显式的 ``127.0.0.1`` / ``localhost``，那是本机中转的
   合法用法）——http 会让 key 在链路上明文传输
2. **拒绝私有网段与云元数据地址**——``169.254.169.254`` 能拿到云厂商的实例凭证，
   那比 OpenRouter key 值钱得多
3. **先解析域名再把连接 pin 到解析出的 IP**——只校验域名挡不住 DNS rebinding：
   校验时解析到公网 IP，真正连接时解析到 127.0.0.1
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

#: 明确允许的本机地址。本机中转（比如同机跑一个 New API）是合法用法。
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class UnsafeUpstreamURL(ValueError):
    """地址不安全。消息里要说清楚**为什么**，否则管理员只会以为工具坏了。"""


@dataclass(frozen=True, slots=True)
class CheckedURL:
    url: str
    host: str
    #: 解析出的 IP。发请求时应当 pin 到它，避免 DNS rebinding。
    addresses: tuple[str, ...]


def _is_blocked(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address, *, allow_private: bool = False
) -> str | None:
    """返回拦截原因，安全则返回 None。

    ``allow_private`` 给 trace 上报地址用。两处的风险不是同一件事：

    - 上游地址会**带着付费 key** 去打，所以内网地址一律拒——那等于把这台机器变成
      一个带凭证的通用内网探针；
    - trace 上报地址的风险是"对话内容被送到不该去的地方"，而**自建 Langfuse 基本
      就在内网**（同一个 docker network、或者 10.x）。一律拒私有网段会把最主流的
      自建部署挡死，而挡死之后人们会去用托管服务——那正好是更差的隐私结果。

    链路本地（含云元数据端点）**两处都拒**。那才是真正危险的目标，而且没有任何
    正当理由把 trace 发到 169.254.169.254。
    """
    # 判定顺序 = 诊断精确度顺序。169.254.0.0/16 同时满足 is_private 与 is_link_local，
    # 而"你指向了云元数据端点"比"你指向了私有网段"信息量大得多——前者说明这台机器
    # 可能正在被用来偷云厂商的实例凭证。更具体的那个必须先判。
    if ip.is_loopback:
        return "回环地址"
    if ip.is_link_local:
        return "链路本地地址（含云元数据端点 169.254.169.254，能拿到实例凭证）"
    if ip.is_private and not allow_private:
        return "私有网段"
    if ip.is_reserved or ip.is_multicast or ip.is_unspecified:
        return "保留 / 组播 / 未指定地址"
    return None


def check_upstream_url(
    raw: str, *, allow_loopback: bool = True, allow_private: bool = False
) -> CheckedURL:
    """校验管理员填写的、**服务端会主动去打**的地址。

    ``allow_loopback`` 默认开：同机跑中转是常见部署。但它必须是**显式**的回环
    主机名，而不是某个域名恰好解析到回环——后者正是 DNS rebinding 的形态。

    ``allow_private`` 见 :func:`_is_blocked`：只有 trace 上报地址该开。
    """
    raw = (raw or "").strip()
    if not raw:
        raise UnsafeUpstreamURL("地址不能为空")

    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"}:
        raise UnsafeUpstreamURL(f"只支持 http/https，收到 {parsed.scheme or '(缺少协议)'!r}")
    if not parsed.hostname:
        raise UnsafeUpstreamURL("地址里没有主机名")

    host = parsed.hostname

    # 显式回环：允许，且不做 DNS 解析（本来就没有可 rebind 的东西）
    if host in LOOPBACK_HOSTS:
        if not allow_loopback:
            raise UnsafeUpstreamURL("不允许指向本机")
        return CheckedURL(url=raw.rstrip("/"), host=host, addresses=(host,))

    # 字面 IP：先判危险地址，**再**判协议。
    #
    # 顺序有意：http://169.254.169.254 同时违反两条，但管理员更需要知道的是
    # "你指向了云元数据端点"而不是"请用 https"——前者说明他可能正在被攻击，
    # 后者只是一个配置疏忽。报错要指向更重要的那个问题。
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        reason = _is_blocked(ip, allow_private=allow_private)
        if reason:
            raise UnsafeUpstreamURL(f"不允许指向{reason}：{host}")
        if parsed.scheme != "https" and not (allow_private and ip.is_private):
            raise UnsafeUpstreamURL("非本机地址必须用 https —— http 会让上游 key 在链路上明文传输")
        return CheckedURL(url=raw.rstrip("/"), host=host, addresses=(host,))

    # 域名：先解析并判危险地址（同样是为了让报错指向更重要的问题），再判协议
    try:
        infos = socket.getaddrinfo(host, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise UnsafeUpstreamURL(f"无法解析域名 {host}：{e.strerror or e}") from e

    addresses = tuple(dict.fromkeys(str(info[4][0]) for info in infos))
    if not addresses:
        raise UnsafeUpstreamURL(f"域名 {host} 没有解析出任何地址")

    for addr in addresses:
        reason = _is_blocked(ipaddress.ip_address(addr), allow_private=allow_private)
        if reason:
            raise UnsafeUpstreamURL(
                f"域名 {host} 解析到了{reason}（{addr}）。这通常意味着 DNS rebinding 攻击，"
                "或者你填的是一个内网地址。"
            )

    if parsed.scheme != "https":
        raise UnsafeUpstreamURL("非本机地址必须用 https —— http 会让上游 key 在链路上明文传输")

    return CheckedURL(url=raw.rstrip("/"), host=host, addresses=addresses)
