"""统一错误信封。

形状是 OpenAI 风格，因为调用方用的是 OpenAI SDK——它按 ``error.type`` 分支。

``type`` 与 ``code`` 分两层：``type`` 是粗粒度闭集（供 SDK 判断该重试还是该报错），
``code`` 可以更细。让两者相等的话，调用方按 ``code`` 写了分支之后就再也细化不了。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from .. import contract as C
from ..contract import ErrorType

log = logging.getLogger(__name__)


def usage_block(usage: Any) -> dict[str, int]:
    """把 ``RunUsage`` 转成 OpenAI 形状的 usage。``None`` 给全零。

    只有这一处定义：成功响应与失败响应必须逐字段同形，否则调用方要写两套解析。
    """
    inp = int(getattr(usage, "input_tokens", 0) or 0)
    out = int(getattr(usage, "output_tokens", 0) or 0)
    return {"prompt_tokens": inp, "completion_tokens": out, "total_tokens": inp + out}


class XingchaError(Exception):
    """所有对外错误的基类。

    ``detail`` 会原样回给调用方，绝不能放上游 URL、header 或任何可能含 key 的东西。
    需要记录细节用 ``log_detail``，它只进日志。
    """

    error_type: ErrorType = ErrorType.INTERNAL_ERROR

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        param: str | None = None,
        log_detail: str | None = None,
        **extra: Any,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or self.error_type.value
        self.param = param
        self.log_detail = log_detail
        self.extra = extra
        #: 这次调用已经产生的用量。失败也要带（契约的 ``USAGE_ON_ERROR``），否则失败
        #: run 的花费不可见。由 ``services/run.map_errors`` 在抛出时挂上——重试耗尽时
        #: 手上没有 result 可读，原地累加的 RunUsage 是唯一还拿得到用量的东西。
        self.usage: Any = None

    @property
    def status_code(self) -> int:
        return C.ERROR_HTTP_STATUS[self.error_type]

    def to_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "message": self.message,
            "type": self.error_type.value,
            "code": self.code,
            "param": self.param,
        }
        body.update(self.extra)
        out: dict[str, Any] = {"error": body}
        # 失败响应也带 usage（契约 §6 的 USAGE_ON_ERROR）：一次重试耗尽的 422 背后是
        # 1+retries 次真实的模型调用，不报出来调用方就看不见自己花了多少。口径与 200
        # 一致（整轮累计），哪些错误要带由 contract.USAGE_ON_ERROR_TYPES 决定，而且零
        # 调用也给 0——形状统一比让调用方分两种情况读 .usage.total_tokens 重要。
        if self.error_type.value in C.USAGE_ON_ERROR_TYPES:
            out["usage"] = usage_block(self.usage)
        return out


# **具体错误。每个只声明 error_type，HTTP 码由契约表决定，不在这里重复。**


class InvalidApiKey(XingchaError):
    """令牌无效 / 禁用 / 过期。对外一律同一条消息——区分等于给公网一个 token 有效性
    oracle。区分只进日志。
    """

    error_type = ErrorType.INVALID_API_KEY

    def __init__(self, log_detail: str | None = None) -> None:
        super().__init__(
            "无效的 API key。请检查 Authorization 头是否为 `Bearer sk-xc-...`。",
            log_detail=log_detail,
        )


class QuotaExceeded(XingchaError):
    error_type = ErrorType.QUOTA_EXCEEDED

    def __init__(self, subject_type: str, window: str, limit_kind: str) -> None:
        super().__init__(
            f"超出配额：{subject_type} 的 {window} {limit_kind} 上限已用尽。",
            subject_type=subject_type,
            window=window,
            limit_kind=limit_kind,
        )


class ModelNotFound(XingchaError):
    error_type = ErrorType.MODEL_NOT_FOUND

    def __init__(self, model: str) -> None:
        super().__init__(
            f"未知的 Agent：{model!r}。用 GET /v1/models 查看可用列表。"
            "（含 `/` 的 model 会被当作上游模型直接转发，不含 `/` 的按 Agent 标识解析。）",
            param="model",
        )


class ModelInvalid(XingchaError):
    error_type = ErrorType.MODEL_INVALID

    def __init__(self, message: str) -> None:
        super().__init__(message, param="model")


class ParamUnsupported(XingchaError):
    error_type = ErrorType.PARAM_UNSUPPORTED

    def __init__(self, param: str) -> None:
        super().__init__(
            f"不支持请求字段 {param!r}。该字段会绕过服务端的运行护栏，因此被明确拒绝"
            "而不是静默忽略。",
            param=param,
        )


class StreamUnsupported(XingchaError):
    error_type = ErrorType.STREAM_UNSUPPORTED

    def __init__(self, model: str) -> None:
        super().__init__(
            f"Agent {model!r} 配置了结构化输出，不支持 stream=true。"
            "流式输出到一半的 JSON 无法被安全解析——诚实报错优于假装支持。"
            "请改用非流式，或改用纯文本 Agent。",
            param="stream",
        )


class RequestTooLarge(XingchaError):
    error_type = ErrorType.REQUEST_TOO_LARGE

    def __init__(self, limit: int = C.MAX_BODY_BYTES) -> None:
        super().__init__(f"请求体超过上限 {limit // 1024 // 1024} MB。", limit_bytes=limit)


class SchemaViolation(XingchaError):
    error_type = ErrorType.SCHEMA_VIOLATION

    def __init__(self, detail: str, retries: int) -> None:
        super().__init__(
            f"模型输出在 {retries} 次重试后仍不符合 schema：{detail}",
            retries=retries,
        )


class AgentSpecInvalid(XingchaError):
    """用户提交的 spec 不合法 → 400。与 :class:`AgentBuildFailed` 分开。"""

    error_type = ErrorType.AGENT_SPEC_INVALID


class AgentBuildFailed(XingchaError):
    """spec 无法构造或无法执行 → 500，需管理员介入。与 400 分开是因为处置路径不同：
    一个让用户改表单，一个让管理员改配置。

    原因要带出来：这类失败每次都发生（不是偶发），原因往往具体又可执行——实测拿到过
    "WebSearchTool is not supported with OpenAIChatModel and model 'x'"。只说"内部错误"
    等于让人去猜一个日志里明写着的答案。脱敏之后再带出去：异常文本常带完整 URL。
    """

    error_type = ErrorType.AGENT_BUILD_FAILED

    def __init__(self, log_detail: str, *, reason: str | None = None) -> None:
        detail = redact((reason or "").strip())[:300]
        super().__init__(
            f"Agent 无法运行：{detail}" if detail else "Agent 定义无法构造，请管理员查看日志。",
            log_detail=log_detail,
        )


class UpstreamError(XingchaError):
    """上游拒了这次请求。``upstream_message`` 是上游自己说的原因，脱敏后原样带给调用方。

    不带的话调用方只看到"上游返回 502"，而真正的原因只在服务端日志里——实测 DeepSeek
    回的是 "Thinking mode does not support this tool_choice"，那是每次都会发生的配置
    问题，看不到那句话的人只会以为网络抖了一下然后一直重试。

    脱敏是必须的：异常文本常带完整 URL、偶尔带 header，直接回显就是一条 key 泄漏路径。
    """

    error_type = ErrorType.UPSTREAM_ERROR

    def __init__(
        self,
        upstream_status: int,
        log_detail: str | None = None,
        upstream_message: str | None = None,
    ) -> None:
        detail = redact(upstream_message.strip())[:300] if upstream_message else ""
        super().__init__(
            f"上游返回 {upstream_status}。{detail}" if detail else f"上游返回 {upstream_status}。",
            upstream_status=upstream_status,
            log_detail=log_detail,
        )


class UpstreamTimeout(XingchaError):
    """单次上游请求超时（``ModelAPIError`` / httpx 超时）。"""

    error_type = ErrorType.UPSTREAM_TIMEOUT

    def __init__(self, seconds: float) -> None:
        super().__init__(f"上游请求超过 {seconds:g} 秒未响应。")


class RequestTimeout(XingchaError):
    """整轮墙钟超时（``asyncio.timeout``）。与 :class:`UpstreamTimeout` 分开：per-Agent
    超时走 ``model_settings['timeout']``，两者来源与排查路径不同，混成一个错误码会让人
    查错方向。
    """

    error_type = ErrorType.REQUEST_TIMEOUT

    def __init__(self, seconds: float) -> None:
        super().__init__(f"整轮调用超过 {seconds:g} 秒未完成。")


# **处理器**

_REDACT_PREFIXES = ("sk-or-v1-", "sk-xc-", "sk-ant-", "sk-proj-")


#: 预编译一次。脱敏跑在**每一条**日志上，不该在热路径里反复编译正则。
#:
#: 保留前缀（``sk-or-v1-***`` 而不是 ``***``）：知道漏的是哪一类 key，才知道该去吊销
#: 哪一把。脱敏的目的是别让密文进日志，不是让日志变得无法排查。
_REDACT_PATTERNS = tuple(
    (re.compile(rf"{re.escape(p)}[A-Za-z0-9_\-]+"), f"{p}***") for p in _REDACT_PREFIXES
)


def redact(text: str) -> str:
    """把可能是 key 的串脱敏。用于日志与任何要外泄的文本。

    异常文本经常带完整 URL、偶尔带 header——直接回显或记日志就是一条 key 泄漏路径。
    """
    for pattern, replacement in _REDACT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFormatter(logging.Formatter):
    """在日志渲染的唯一收口上脱敏。

    必须是 Formatter，不能在各个 log 调用点手动 redact：手动调漏过一次——
    :func:`unhandled_error_handler` 里的 ``log.exception()`` 把整条 traceback 原样写了
    出去，响应体干净而日志里那把 key 逐字出现，而日志会进 `docker logs` 与任何日志收集。

    Filter 也不够：traceback 是 handler 阶段由 Formatter 渲染的，Filter 拿到的
    ``record.exc_info`` 还是个元组，改不动最终文本。
    """

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


async def xingcha_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, XingchaError)
    if exc.log_detail:
        log.warning("%s: %s", exc.error_type.value, redact(exc.log_detail))
    return JSONResponse(status_code=exc.status_code, content=exc.to_body())


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """兜底：5xx 对外**只给固定文案 + run_id**，细节只进日志。

    直接回显异常文本是最常见的一条上游 key 泄漏路径。
    """
    run_id = getattr(request.state, "run_id", None)
    log.exception("未处理的异常 run_id=%s", run_id)
    body = {
        "error": {
            "message": C.INTERNAL_ERROR_MESSAGE,
            "type": ErrorType.INTERNAL_ERROR.value,
            "code": ErrorType.INTERNAL_ERROR.value,
            "param": None,
        }
    }
    if run_id:
        body["error"]["run_id"] = run_id
    return JSONResponse(status_code=500, content=body)
