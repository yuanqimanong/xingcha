"""OTel trace 装配。

------------------------------------------------------------------------------
它解决的是哪个问题
------------------------------------------------------------------------------

管理面的运行列表回答"跑了几次、花了多少、有没有报错"。它回答不了**"模型到底看到了
什么、又吐回了什么"**——而调 Agent 的时间几乎全花在这个问题上：提示词改了一版，
输出变差了，为什么？

pydantic-ai 自带 OTel 埋点，把每次模型请求的消息与响应都记成 span 属性。所以这里
要做的不是"实现可观测"，而是**把 SDK 装好、把导出接上**，再补一层星槎自己的 span
把 ``run_id`` 带进去——不然 trace 与运行记录两边对不上，排查时只能靠时间戳猜。

------------------------------------------------------------------------------
三条不能违反的规矩
------------------------------------------------------------------------------

1. **默认关。** 打开意味着提示词与模型输出会离开这台机器。这个项目存在的理由就是
   不想让请求经过别人手里，所以这件事必须是一次显式的决定。
2. **装配失败不能影响服务。** endpoint 写错、证书过期、Langfuse 挂了——一律降级成
   "没有 trace"，绝不降级成"服务起不来"。
3. **关停要 flush。** BatchSpanProcessor 攒批后台发，不 flush 就会丢掉最后那批——
   而"升级前最后那几次调用"恰好是排查升级问题时最想看的。
"""

from __future__ import annotations

import base64
import contextlib
import logging
from collections.abc import Iterator
from typing import Any

log = logging.getLogger(__name__)

#: span 属性的名字只在这里出现。散在各调用点的话，两条路径迟早写出
#: ``xc.run_id`` 与 ``xingcha.run_id`` 两套名字，而看板是按名字聚合的。
ATTR_RUN_ID = "xingcha.run_id"
ATTR_KIND = "xingcha.kind"
ATTR_MODEL = "xingcha.model"
ATTR_TIER = "xingcha.tier"
ATTR_STATUS = "xingcha.status"
ATTR_ERROR_TYPE = "xingcha.error_type"
ATTR_COST_USD = "xingcha.cost_usd"
ATTR_COST_SOURCE = "xingcha.cost_source"
ATTR_SCHEMA_VIOLATIONS = "xingcha.schema_violations"
ATTR_SCHEMA_RETRIES = "xingcha.schema_retries"


class Tracing:
    """已装配好的 trace 管道。"""

    __slots__ = ("_processor", "endpoint", "include_content", "provider", "tracer")

    def __init__(self, provider: Any, processor: Any, *, endpoint: str, include_content: bool):
        #: pydantic-ai 的 ``InstrumentationSettings`` 要直接拿它，所以是公开的。
        self.provider = provider
        self._processor = processor
        self.endpoint = endpoint
        self.include_content = include_content
        self.tracer = provider.get_tracer("xingcha")

    def shutdown(self) -> None:
        """flush 并关闭。**关停序列里必须调**，否则最后那批 span 丢掉。"""
        try:
            self._processor.force_flush(timeout_millis=3000)
        except Exception:
            log.warning("trace flush 失败，最后一批可能丢了", exc_info=True)
        with contextlib.suppress(Exception):
            self.provider.shutdown()


def langfuse_headers(public_key: str, secret_key: str) -> dict[str, str]:
    """Langfuse 的 OTLP 入口用 HTTP Basic。

    这里直接拼而不是让用户自己 base64：让人手算 base64 再贴进配置，是一个必然会
    出错、而且错了之后只表现为"trace 没上去"的步骤。
    """
    raw = f"{public_key}:{secret_key}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}


def setup(
    *,
    endpoint: str,
    headers: dict[str, str] | None = None,
    service_name: str = "xingcha",
    include_content: bool = True,
    timeout_seconds: float = 10.0,
) -> Tracing | None:
    """装配 OTLP/HTTP 导出。失败返回 ``None``，绝不抛。

    ``endpoint`` 要给完整的 traces 入口，例如
    ``https://cloud.langfuse.com/api/public/otel/v1/traces``。
    """
    if not endpoint:
        return None
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        exporter = OTLPSpanExporter(
            endpoint=endpoint, headers=headers or None, timeout=timeout_seconds
        )
        provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        processor = BatchSpanProcessor(exporter)
        provider.add_span_processor(processor)
    except Exception:
        # 装配失败只丢可观测，不丢服务。**这条不能改成抛**：一个写错的 endpoint
        # 会变成"服务起不来"，而它本来只该是"看不到 trace"。
        log.warning("trace 装配失败，已降级为不上报", exc_info=True)
        return None

    log.info("trace 已启用 → %s（含内容：%s）", endpoint, include_content)
    return Tracing(provider, processor, endpoint=endpoint, include_content=include_content)


# =============================================================================
# 调用点用的门面
# =============================================================================


@contextlib.contextmanager
def run_span(
    tracing: Tracing | None, *, kind: str, run_id: str, model: str
) -> Iterator[Any | None]:
    """一次调用的 span。``tracing`` 为 None 时是零开销的空操作。

    用 ``start_as_current_span`` 而不是自己管 context：pydantic-ai 的 span 靠环境
    context 找父节点，而 context 是 contextvar（按任务隔离）。手动 attach/detach
    一旦跨了 await 边界就会出现"detach 了别人的 token"这类难查的问题。词法作用域
    是免费的正确性——代价只是调用点得放在工作真正发生的地方。
    """
    if tracing is None:
        yield None
        return
    with tracing.tracer.start_as_current_span(f"xingcha.{kind}") as span:
        span.set_attribute(ATTR_RUN_ID, run_id)
        span.set_attribute(ATTR_KIND, kind)
        span.set_attribute(ATTR_MODEL, model)
        yield span


def record_outcome(
    span: Any | None,
    *,
    status: str,
    error_type: str | None = None,
    tier: str | None = None,
    cost_usd: Any = None,
    cost_source: str | None = None,
    schema_violations: int = 0,
    schema_retries: int = 0,
) -> None:
    """把一次调用的结论写到 span 上。

    收显式关键字而不是"随便给个对象让我 getattr"：命令式路径手上是 ``RunRecord``，
    流式路径手上是 ``RunOutcome``，两者字段名不完全一样。鸭子类型在这里的结果是
    某一路悄悄少记几个属性而没人发现——看板上表现为"有些运行没有费用"。

    **不重算任何东西。** span 与 run 行必须是同一份事实的两个视图；这里自己算一遍
    费用的话，看板和账单就会给出两个不同的数字。
    """
    if span is None:
        return
    with contextlib.suppress(Exception):
        span.set_attribute(ATTR_STATUS, status)
        if error_type:
            span.set_attribute(ATTR_ERROR_TYPE, error_type)
        if tier:
            span.set_attribute(ATTR_TIER, str(tier))
        if cost_usd is not None:
            span.set_attribute(ATTR_COST_USD, str(cost_usd))
        if cost_source:
            span.set_attribute(ATTR_COST_SOURCE, cost_source)
        if schema_violations:
            span.set_attribute(ATTR_SCHEMA_VIOLATIONS, schema_violations)
        if schema_retries:
            span.set_attribute(ATTR_SCHEMA_RETRIES, schema_retries)


def record_run(span: Any | None, rec: Any) -> None:
    """``RunRecord`` 版的 :func:`record_outcome`。"""
    record_outcome(
        span,
        status=rec.status or "",
        error_type=rec.error_type,
        tier=rec.tier,
        cost_usd=rec.cost_usd,
        cost_source=rec.cost_source,
        schema_violations=rec.schema_violations or 0,
        schema_retries=rec.schema_retries or 0,
    )
