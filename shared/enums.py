"""状态枚举与合法转移白名单。

权威来源：docs/20-系统设计/数据模型.md §4、系统设计说明书 §5.3。
枚举一律以字符串字面值存库（不存整型），便于直读数据库。
"""

from __future__ import annotations

# ---------- 角色 ----------
ROLE_STUDENT = "student"
ROLE_ADMIN = "admin"
ROLES = (ROLE_STUDENT, ROLE_ADMIN)

# ---------- 文档解析状态 ----------
PARSE_UPLOADED = "uploaded"
PARSE_PARSING = "parsing"
PARSE_PARSED = "parsed"
PARSE_EMPTY = "empty"
PARSE_FAILED = "failed"
PARSE_CHUNKED = "chunked"
PARSE_INDEXED = "indexed"

PARSE_STATUSES = (
    PARSE_UPLOADED, PARSE_PARSING, PARSE_PARSED,
    PARSE_EMPTY, PARSE_FAILED, PARSE_CHUNKED, PARSE_INDEXED,
)

# 白名单：只有这里列出的 (from, to) 才允许写入
PARSE_TRANSITIONS: dict[str, tuple[str, ...]] = {
    PARSE_UPLOADED: (PARSE_PARSING,),
    PARSE_PARSING: (PARSE_PARSED, PARSE_EMPTY, PARSE_FAILED),
    # parsed/chunked 允许回到 parsing：管理员强制重建解析时必须能从任意
    # 中间态重来，否则只能先"复位到 uploaded"，等于绕过状态机（M3 修正）
    PARSE_PARSED: (PARSE_CHUNKED, PARSE_PARSING),
    PARSE_CHUNKED: (PARSE_INDEXED, PARSE_PARSING),
    PARSE_EMPTY: (PARSE_PARSING,),
    PARSE_FAILED: (PARSE_PARSING,),
    PARSE_INDEXED: (PARSE_PARSING,),
}


def is_parse_transition_allowed(current: str, target: str) -> bool:
    """状态机守卫。非法转移应在服务层抛 409 PARSE_IN_PROGRESS 之外的状态码。"""
    return target in PARSE_TRANSITIONS.get(current, ())


# ---------- 文档时效状态 ----------
EFFECTIVE_ACTIVE = "active"
EFFECTIVE_SUPERSEDED = "superseded"
EFFECTIVE_EXPIRED = "expired"
EFFECTIVE_UNKNOWN = "unknown"

EFFECTIVE_STATUSES = (
    EFFECTIVE_ACTIVE, EFFECTIVE_SUPERSEDED, EFFECTIVE_EXPIRED, EFFECTIVE_UNKNOWN,
)

# ---------- 文件类型 ----------
FILE_TYPE_BY_EXT = {
    "pdf": "pdf",
    "docx": "docx",
    "txt": "txt",
    "md": "md",
}
SUPPORTED_EXTENSIONS = tuple(FILE_TYPE_BY_EXT)

# 魔数：用于识别"改名的可执行文件"（T-01）
MAGIC_PDF = b"%PDF-"
MAGIC_ZIP = b"PK\x03\x04"      # docx 是 zip 容器

# ---------- 检索模式（/health 的 embedding_mode 与问答响应的 mode 同源）----------
EMBEDDING_VECTOR = "vector"        # 向量检索可用
EMBEDDING_BM25_ONLY = "bm25_only"  # 仅关键词检索（未配置 embedding）
EMBEDDING_DISABLED = "disabled"    # 完全不可用

# ---------- 回答置信度（模型自评，非绝对可靠，仅作提示）----------
CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"
CONFIDENCE_VALUES = (CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, CONFIDENCE_LOW)


# ---------- M5：规则类型与算子（数据模型 §4.2、说明书 §4.4）----------
RULE_ELIGIBILITY = "eligibility"
RULE_MATERIAL = "material"
RULE_DEADLINE = "deadline"
RULE_PROCESS = "process"
RULE_RESTRICTION = "restriction"
RULE_TYPES = (RULE_ELIGIBILITY, RULE_MATERIAL, RULE_DEADLINE, RULE_PROCESS, RULE_RESTRICTION)

# 只有这两类参与"资格三态判定"，其余三类用于生成计划任务。
# 混在一起统计会让"材料清单"被算成"不符合条件"，这是口径错误不是设计选择。
CONDITION_RULE_TYPES = (RULE_ELIGIBILITY, RULE_RESTRICTION)

OP_EQUALS = "equals"
OP_NOT_EQUALS = "not_equals"
OP_IN = "in"
OP_GTE = "gte"
OP_LTE = "lte"
OP_EXISTS = "exists"
OP_CONTAINS_ANY = "contains_any"
OP_MANUAL_REVIEW = "manual_review"
RULE_OPERATORS = (
    OP_EQUALS, OP_NOT_EQUALS, OP_IN, OP_GTE, OP_LTE,
    OP_EXISTS, OP_CONTAINS_ANY, OP_MANUAL_REVIEW,
)
# 需要 expected_value 才能求值的算子；缺了就判 ambiguous
NEEDS_EXPECTED = (OP_EQUALS, OP_NOT_EQUALS, OP_IN, OP_GTE, OP_LTE, OP_CONTAINS_ANY)

# ---------- 复核状态 ----------
REVIEW_NEEDS = "needs_review"
REVIEW_APPROVED = "approved"
REVIEW_REJECTED = "rejected"
REVIEW_STATUSES = (REVIEW_NEEDS, REVIEW_APPROVED, REVIEW_REJECTED)

EXTRACT_SUGGESTED = "suggested"
EXTRACT_VERIFIED = "verified"
EXTRACTION_STATUSES = (EXTRACT_SUGGESTED, EXTRACT_VERIFIED)

# ---------- 三态匹配 ----------
MATCH_MET = "met"
MATCH_NOT_MET = "not_met"
MATCH_UNKNOWN = "unknown"
MATCH_STATUSES = (MATCH_MET, MATCH_NOT_MET, MATCH_UNKNOWN)

UNKNOWN_MISSING_INFO = "missing_info"
UNKNOWN_AMBIGUOUS_RULE = "ambiguous_rule"
UNKNOWN_NO_EVIDENCE = "no_evidence"
UNKNOWN_CONFLICTING = "conflicting_policy"
UNKNOWN_REASONS = (
    UNKNOWN_MISSING_INFO, UNKNOWN_AMBIGUOUS_RULE, UNKNOWN_NO_EVIDENCE, UNKNOWN_CONFLICTING,
)

# ---------- 计划状态 ----------
PLAN_DRAFT = "draft"
PLAN_CONFIRMED = "confirmed"
PLAN_ARCHIVED = "archived"
PLAN_REJECTED = "rejected"
PLAN_STATUSES = (PLAN_DRAFT, PLAN_CONFIRMED, PLAN_ARCHIVED, PLAN_REJECTED)

# 确认只能走 /plans/{id}/confirm（那里才校验依赖与幂等），
# 因此 PATCH 白名单里没有 draft -> confirmed 这条边。
PLAN_TRANSITIONS: dict[str, tuple[str, ...]] = {
    PLAN_DRAFT: (PLAN_ARCHIVED,),
    PLAN_CONFIRMED: (PLAN_ARCHIVED,),
    PLAN_ARCHIVED: (),
    PLAN_REJECTED: (),
}


def is_plan_transition_allowed(current: str, target: str) -> bool:
    return target in PLAN_TRANSITIONS.get(current, ())


# ---------- 任务状态 ----------
TASK_TODO = "todo"
TASK_DOING = "doing"
TASK_DONE = "done"
TASK_DEFERRED = "deferred"
TASK_CANCELLED = "cancelled"
TASK_STATUSES = (TASK_TODO, TASK_DOING, TASK_DONE, TASK_DEFERRED, TASK_CANCELLED)

TASK_TRANSITIONS: dict[str, tuple[str, ...]] = {
    TASK_TODO: (TASK_DOING, TASK_DONE, TASK_DEFERRED, TASK_CANCELLED),
    TASK_DOING: (TASK_DONE, TASK_DEFERRED, TASK_CANCELLED),
    TASK_DEFERRED: (TASK_TODO, TASK_CANCELLED),
    # 撤销完成只能回到 todo：允许 done -> doing 会让"完成时间"变得无法解释
    TASK_DONE: (TASK_TODO,),
    TASK_CANCELLED: (),
}


def is_task_transition_allowed(current: str, target: str) -> bool:
    return target in TASK_TRANSITIONS.get(current, ())


PRIORITY_P0 = "P0"
PRIORITY_P1 = "P1"
PRIORITY_P2 = "P2"
PRIORITY_P3 = "P3"
PRIORITIES = (PRIORITY_P0, PRIORITY_P1, PRIORITY_P2, PRIORITY_P3)

# ---------- 画像字段白名单（数据模型 §4.4）----------
# 模型与前端都不得引入白名单外的字段：`family_income`、`id_card` 这类字段
# 一旦入库，就是"多采集了一份个人信息"，删库都不算挽回。
PROFILE_FIELDS = (
    "student_type", "grade", "gpa", "rank_percent", "disciplinary_record",
    "poverty_certified", "major", "credits_earned", "enrollment_status",
)

# 规则抽取里允许出现的 field_key（与上面同一白名单，单独命名便于阅读）
RULE_FIELD_KEYS = PROFILE_FIELDS
