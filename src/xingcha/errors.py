"""统一错误信封。

形状是 OpenAI 风格，因为调用方用的是 OpenAI SDK——它按 ``error.type`` 分支。

``type`` 与 ``code`` **分两层**：``type`` 是粗粒度闭集（供 SDK 判断该重试还是该报错），
``code`` 可以更细。两者相等的设计是发出第一个错误响应之后就再也回不去的冻结——
一旦调用方按 ``code`` 写了分支，你就不能再细化它了。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from . import contract as C
from .contract import ErrorType

log = logging.getLogger(__name__)


class XingchaError(Exception):
    """所有对外错误的基类。

    ``detail`` 里的内容会**原样回给调用方**，所以绝不能放上游 URL、header 或任何
    可能含 key 的东西。需要记录细节就用 ``log_detail``，它只进日志。
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
        return {"error": body}


# --------------------------------------------------------------------------
# 具体错误。每个只声明 error_type，HTTP 码由契约表决定，不在这里重复。
# --------------------------------------------------------------------------


class InvalidApiKey(XingchaError):
    """令牌无效 / 禁用 / 过期。

    **对外一律同一条消息。** 区分它们等于给公网一个 token 有效性 oracle
    （"这个 key 存在但过期了"是白送的信息）。区分只进日志。
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
    """spec 无法构造 → 500，通常是上游版本变动，需管理员介入。

    与 400 分开是因为处置路径完全不同：一个是让用户改表单，一个是让管理员看日志。
    """

    error_type = ErrorType.AGENT_BUILD_FAILED

    def __init__(self, log_detail: str) -> None:
        super().__init__(
            "Agent 定义无法构造。通常是 pydantic-ai 版本变动导致的，请管理员查看日志。",
            log_detail=log_detail,
        )


class UpstreamError(XingchaError):
    error_type = ErrorType.UPSTREAM_ERROR

    def __init__(self, upstream_status: int, log_detail: str | None = None) -> None:
        super().__init__(
            f"上游返回 {upstream_status}。",
            upstream_status=upstream_status,
            log_detail=log_detail,
        )


class UpstreamTimeout(XingchaError):
    """单次上游请求超时（``ModelAPIError`` / httpx 超时）。"""

    error_type = ErrorType.UPSTREAM_TIMEOUT

    def __init__(self, seconds: float) -> None:
        super().__init__(f"上游请求超过 {seconds:g} 秒未响应。")


class RequestTimeout(XingchaError):
    """整轮墙钟超时（``asyncio.timeout``）。

    与 :class:`UpstreamTimeout` 分开：``Agent.run`` 没有 timeout 参数，per-Agent 超时
    走 ``model_settings['timeout']``，整轮上限只能靠 ``asyncio.timeout``。两者来源不同、
    排查路径也不同，混成一个错误码会让人查错方向。
    """

    error_type = ErrorType.REQUEST_TIMEOUT

    def __init__(self, seconds: float) -> None:
        super().__init__(f"整轮调用超过 {seconds:g} 秒未完成。")


# --------------------------------------------------------------------------
# 处理器
# --------------------------------------------------------------------------

_REDACT_PREFIXES = ("sk-or-v1-", "sk-xc-", "sk-ant-", "sk-proj-")


def redact(text: str) -> str:
    """把可能是 key 的串脱敏。用于日志与任何要外泄的文本。

    异常文本经常带完整 URL、偶尔带 header——直接回显或记日志就是一条 key 泄漏路径。
    """
    import re

    out = text
    for prefix in _REDACT_PREFIXES:
        out = re.sub(rf"{re.escape(prefix)}[A-Za-z0-9_\-]+", f"{prefix}***", out)
    return out


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
