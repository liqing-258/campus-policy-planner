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
RULE_NOT_FOUND = 2003        # 404 规则不存在
PLAN_NOT_FOUND = 2004        # 404 计划/任务不存在
STATE_TRANSITION_INVALID = 2005  # 409 状态转移非法
DEPENDENCY_CYCLE = 2006      # 409 任务依赖成环
PARSE_IN_PROGRESS = 2007     # 409 解析进行中
RULES_NOT_READY = 2008       # 200 没有已复核规则，无法匹配（业务态）

UNAUTHENTICATED = 3001       # 401 未登录或令牌过期
FORBIDDEN = 3002             # 403 无权限

# 4xxx：AI 链路。前三个是**业务态**，HTTP 保持 200（见契约规范 §3.3）
INSUFFICIENT_EVIDENCE = 4001  # 200 证据不足，拒答
LLM_OUTPUT_INVALID = 4002     # 502 模型输出结构非法（重试后仍失败）
CITATION_REJECTED = 4003      # 200 引用越界，已剔除
LLM_TIMEOUT = 4004           # 504 模型超时
LLM_UPSTREAM_ERROR = 4005    # 502 上游 5xx / 鉴权失败
DEGRADED_MODE = 4006         # 200 已降级（无 Key / 仅 BM25）

INTERNAL_ERROR = 9001        # 500 未预期异常
DB_UNAVAILABLE = 9002        # 503 数据库不可用

# 业务态错误码：HTTP 必须 200，前端按 code 渲染提示卡而非报错
BUSINESS_STATE_CODES = frozenset({
    INSUFFICIENT_EVIDENCE, CITATION_REJECTED, DEGRADED_MODE, RULES_NOT_READY,
})

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
    RULE_NOT_FOUND: 404,
    PLAN_NOT_FOUND: 404,
    STATE_TRANSITION_INVALID: 409,
    DEPENDENCY_CYCLE: 409,
    PARSE_IN_PROGRESS: 409,
    RULES_NOT_READY: 200,
    UNAUTHENTICATED: 401,
    FORBIDDEN: 403,
    INSUFFICIENT_EVIDENCE: 200,
    LLM_OUTPUT_INVALID: 502,
    CITATION_REJECTED: 200,
    LLM_TIMEOUT: 504,
    LLM_UPSTREAM_ERROR: 502,
    DEGRADED_MODE: 200,
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
    RULE_NOT_FOUND: "规则不存在",
    PLAN_NOT_FOUND: "计划或任务不存在",
    STATE_TRANSITION_INVALID: "状态转移非法",
    DEPENDENCY_CYCLE: "任务依赖成环，无法确认计划",
    PARSE_IN_PROGRESS: "解析进行中，请稍后再试",
    RULES_NOT_READY: "该文件还没有已复核的规则，无法匹配；请管理员先抽取并复核规则",
    UNAUTHENTICATED: "未登录或登录已过期",
    FORBIDDEN: "没有权限执行该操作",
    INSUFFICIENT_EVIDENCE: "未在政策库中找到足够依据，无法给出可靠回答",
    LLM_OUTPUT_INVALID: "模型返回内容结构不符合约定，已重试仍失败",
    CITATION_REJECTED: "部分引用无法对应到原文，已剔除",
    LLM_TIMEOUT: "模型响应超时",
    LLM_UPSTREAM_ERROR: "模型服务暂时不可用",
    DEGRADED_MODE: "当前为降级模式：未接入模型，仅返回原文片段",
    INTERNAL_ERROR: "服务器内部错误",
    DB_UNAVAILABLE: "数据库暂时不可用",
}
