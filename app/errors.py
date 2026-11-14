"""异常体系。

设计原则：**服务层只抛业务异常，路由层不写 try/except**。
所有异常在 app/contract.py 注册的处理器里被统一翻译成响应外壳，
因此 `message` 一定可直接展示给用户，且绝不包含堆栈。
"""

from __future__ import annotations

from shared import codes
from shared.codes import CODE_HTTP, CODE_MESSAGES


class ApiError(Exception):
    """业务异常基类。子类只需声明 code，HTTP 状态由 CODE_HTTP 推导。

    `payload` 是可选的结构化附加数据。业务态异常（4001/4003/4006）**必须**带 payload：
    拒答时要回传检索到的候选片段供用户自行查看，降级时要回传原文片段。
    没有它，前端只能显示一句"证据不足"，用户无法判断是"库里真没有"还是"系统坏了"。
    """

    code: int = codes.INVALID_PARAM
    http_status: int | None = None
    default_message: str | None = None
    payload: dict | None = None

    def __init__(self, message: str | None = None, *, code: int | None = None,
                 http_status: int | None = None, payload: dict | None = None) -> None:
        if code is not None:
            self.code = code
        if http_status is not None:
            self.http_status = http_status
        if payload is not None:
            self.payload = payload
        self.message = (
            message
            or self.default_message
            or CODE_MESSAGES.get(self.code, "请求处理失败")
        )
        super().__init__(self.message)

    @property
    def status(self) -> int:
        if self.http_status is not None:
            return self.http_status
        return CODE_HTTP.get(self.code, 400)

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


# ---------- 参数与校验 ----------
class InvalidParam(ApiError):
    code = codes.INVALID_PARAM


class CaptchaInvalid(ApiError):
    """数字验证码错误或过期。

    与 `InvalidParam` 分开，是因为前端要据此**换一张图**——参数格式错只需提示，
    验证码错必须让用户重看新码（服务端一码一用，失败即销毁）。
    """

    code = codes.CAPTCHA_INVALID


class UserDuplicated(ApiError):
    """学号重复。比 `DocumentDuplicated` 多一个用途：注册页要把链接改成"去登录"。"""

    code = codes.USER_DUPLICATED


class InvalidPagination(ApiError):
    code = codes.INVALID_PAGINATION


class SchemaInvalid(ApiError):
    code = codes.SCHEMA_INVALID


# ---------- 文件 ----------
class FileTooLarge(ApiError):
    code = codes.FILE_TOO_LARGE


class FileTypeUnsupported(ApiError):
    code = codes.FILE_TYPE_UNSUPPORTED


# ---------- 资源 ----------
class DocumentNotFound(ApiError):
    code = codes.DOCUMENT_NOT_FOUND


class DocumentDuplicated(ApiError):
    code = codes.DOCUMENT_DUPLICATED


class ParseInProgress(ApiError):
    code = codes.PARSE_IN_PROGRESS


# ---------- M5：规则 / 匹配 / 计划 / 任务 ----------
class RuleNotFound(ApiError):
    code = codes.RULE_NOT_FOUND


class PlanNotFound(ApiError):
    code = codes.PLAN_NOT_FOUND


class StateTransitionInvalid(ApiError):
    code = codes.STATE_TRANSITION_INVALID


class DependencyCycle(ApiError):
    code = codes.DEPENDENCY_CYCLE


class RulesNotReady(ApiError):
    """有文档但没有任何已复核规则。业务态：HTTP 200，不是错误。"""

    code = codes.RULES_NOT_READY


# ---------- M6：消息与工作流 ----------
class MessageNotFound(ApiError):
    """消息或工作流记录不存在。

    与 `DocumentNotFound` 分开，是为了让"点开一条已删除的答案反馈"报出准确的
    对象类型——都报"文档不存在"会让用户去查文档列表，而问题在消息上。
    """

    code = codes.MESSAGE_NOT_FOUND


# ---------- 权限 ----------
class Unauthenticated(ApiError):
    code = codes.UNAUTHENTICATED


class Forbidden(ApiError):
    code = codes.FORBIDDEN


# ---------- AI 链路（4001/4003/4006 是业务态，HTTP 保持 200）----------
class InsufficientEvidence(ApiError):
    """证据不足 → 拒答。必须带 payload：候选片段 + 拒答原因，供用户自行查阅。"""

    code = codes.INSUFFICIENT_EVIDENCE


class LlmOutputInvalid(ApiError):
    """模型输出结构非法（重试后仍失败）。

    `retriable=False` 用于**重试也没用**的场合——典型是 max_tokens 被推理耗尽导致正文为空：
    参数不变的情况下再试一次必然同样空，白等一个 RTT 还可能多花一次 token 钱。
    """

    code = codes.LLM_OUTPUT_INVALID
    retriable = True

    def __init__(self, message: str | None = None, *, retriable: bool | None = None,
                 **kwargs) -> None:
        if retriable is not None:
            self.retriable = retriable
        super().__init__(message, **kwargs)


class CitationRejected(ApiError):
    code = codes.CITATION_REJECTED


class LlmTimeout(ApiError):
    code = codes.LLM_TIMEOUT


class LlmUpstreamError(ApiError):
    code = codes.LLM_UPSTREAM_ERROR


class DegradedMode(ApiError):
    code = codes.DEGRADED_MODE


# ---------- 系统 ----------
class InternalError(ApiError):
    code = codes.INTERNAL_ERROR


class DbUnavailable(ApiError):
    code = codes.DB_UNAVAILABLE
