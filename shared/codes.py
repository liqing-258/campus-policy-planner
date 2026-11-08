"""错误码常量与 HTTP 映射。

唯一权威来源是 docs/30-接口契约/接口契约统一规范.md §3.2。
本文件只被 app/errors.py 与测试引用；新增错误码必须先改文档，再改这里。
"""

from __future__ import annotations

# ---------- 业务码 ----------
OK = 0
INVALID_PARAM = 1001         # 400 参数非法
INVALID_PAGINATION = 1002    # 400 分页参数非法
SCHEMA_INVALID = 1003        # 422 结构校验失败
FILE_TOO_LARGE = 1004        # 413 文件超限
FILE_TYPE_UNSUPPORTED = 1005  # 415 类型不支持

DOCUMENT_DUPLICATED = 2001   # 409 文档已存在
DOCUMENT_NOT_FOUND = 2002    # 404 文档不存在
PARSE_IN_PROGRESS = 2007     # 409 解析进行中

UNAUTHENTICATED = 3001       # 401 未登录或令牌过期
FORBIDDEN = 3002             # 403 无权限

INTERNAL_ERROR = 9001        # 500 未预期异常
DB_UNAVAILABLE = 9002        # 503 数据库不可用

# ---------- 业务码 → HTTP 状态 ----------
CODE_HTTP: dict[int, int] = {
    OK: 200,
    INVALID_PARAM: 400,
    INVALID_PAGINATION: 400,
    SCHEMA_INVALID: 422,
    FILE_TOO_LARGE: 413,
    FILE_TYPE_UNSUPPORTED: 415,
    DOCUMENT_DUPLICATED: 409,
    DOCUMENT_NOT_FOUND: 404,
    PARSE_IN_PROGRESS: 409,
    UNAUTHENTICATED: 401,
    FORBIDDEN: 403,
    INTERNAL_ERROR: 500,
    DB_UNAVAILABLE: 503,
}

# ---------- 默认文案（调用方未给 message 时使用，需可直接展示给用户）----------
CODE_MESSAGES: dict[int, str] = {
    OK: "success",
    INVALID_PARAM: "参数校验失败",
    INVALID_PAGINATION: "分页参数非法",
    SCHEMA_INVALID: "请求体结构校验失败",
    FILE_TOO_LARGE: "文件超过大小限制",
    FILE_TYPE_UNSUPPORTED: "文件类型不支持",
    DOCUMENT_DUPLICATED: "文档已存在",
    DOCUMENT_NOT_FOUND: "文档不存在",
    PARSE_IN_PROGRESS: "解析进行中，请稍后再试",
    UNAUTHENTICATED: "未登录或登录已过期",
    FORBIDDEN: "没有权限执行该操作",
    INTERNAL_ERROR: "服务器内部错误",
    DB_UNAVAILABLE: "数据库暂时不可用",
}
