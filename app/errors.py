"""异常体系。

设计原则：**服务层只抛业务异常，路由层不写 try/except**。
所有异常在 app/contract.py 注册的处理器里被统一翻译成响应外壳，
因此 `message` 一定可直接展示给用户，且绝不包含堆栈。
"""

from __future__ import annotations

from shared import codes
from shared.codes import CODE_HTTP, CODE_MESSAGES


class ApiError(Exception):
    """业务异常基类。子类只需声明 code，HTTP 状态由 CODE_HTTP 推导。"""

    code: int = codes.INVALID_PARAM
    http_status: int | None = None
    default_message: str | None = None

    def __init__(self, message: str | None = None, *, code: int | None = None,
                 http_status: int | None = None) -> None:
        if code is not None:
            self.code = code
        if http_status is not None:
            self.http_status = http_status
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


# ---------- 权限 ----------
class Unauthenticated(ApiError):
    code = codes.UNAUTHENTICATED


class Forbidden(ApiError):
    code = codes.FORBIDDEN


# ---------- 系统 ----------
class InternalError(ApiError):
    code = codes.INTERNAL_ERROR


class DbUnavailable(ApiError):
    code = codes.DB_UNAVAILABLE
