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

# ---------- R14：校园日历 ----------
# 六类日程。**存库的只有个人日程的 category**（`calendar_events.category`）；
# 任务侧的类别按 `rule_type + 所属文档标题` 实时推断，不落库：
# 规则重新抽取、文件换版之后，落库的分类会立刻变成过期数据，
# 而推断每次都是按当前规则算的。这条与 R13「不建推荐表」同源。
CAL_SCHOLARSHIP = "scholarship"   # 奖学金 / 助学金申请截止
CAL_AWARD = "award"               # 评奖评优
CAL_EXAM = "exam"                 # 考试报名
CAL_MATERIAL = "material"         # 材料提交
CAL_LIBRARY = "library"           # 图书借阅与归还
CAL_PERSONAL = "personal"         # 学生自己添加的个人事务
CAL_OTHER = "other"               # 认不出类别的政策事务（兜底，不硬塞进上面五类）

CALENDAR_CATEGORIES = (
    CAL_SCHOLARSHIP, CAL_AWARD, CAL_EXAM,
    CAL_MATERIAL, CAL_LIBRARY, CAL_PERSONAL, CAL_OTHER,
)

CALENDAR_CATEGORY_LABELS = {
    CAL_SCHOLARSHIP: "奖学金截止",
    CAL_AWARD: "评奖评优",
    CAL_EXAM: "考试报名",
    CAL_MATERIAL: "材料提交",
    CAL_LIBRARY: "图书归还",
    CAL_PERSONAL: "个人事务",
    CAL_OTHER: "其他事务",
}

# 日期来源三态。**不猜年份**这条要求在这里的落点：只有拿到日期的条目
# 才谈得上"日期从哪来"；没有日期的一律进"待确认"清单，不进日历格子。
DATE_SOURCE_POLICY = "policy"        # 政策原文给了完整年月日
DATE_SOURCE_CONFIRMED = "confirmed"  # 原文只给月日，学生补了年份
DATE_SOURCE_MANUAL = "manual"        # 学生完全自己定的
DATE_SOURCES = (DATE_SOURCE_POLICY, DATE_SOURCE_CONFIRMED, DATE_SOURCE_MANUAL)

DATE_SOURCE_LABELS = {
    DATE_SOURCE_POLICY: "政策原文",
    DATE_SOURCE_CONFIRMED: "你补全年份",
    DATE_SOURCE_MANUAL: "手动填写",
}

# 没有日期时"缺的到底是什么"。三种缺法必须给三种说法：
# 混成一句"未设置日期"，会让「原文给了 9 月 10 日、只缺年份」看起来像
# 「原文一个日期都没提」——前者只差一个数字，后者要学生自己去问学院。
UNDATED_YEAR_MISSING = "year_missing"   # 原文有月日、缺年份 —— 绝不能自己补
UNDATED_NO_DATE = "no_date_in_policy"   # 原文一个日期都没给
UNDATED_NOT_SET = "not_set"             # 这条任务本来就没要求日期
UNDATED_REASONS = (UNDATED_YEAR_MISSING, UNDATED_NO_DATE, UNDATED_NOT_SET)

UNDATED_REASON_LABELS = {
    UNDATED_YEAR_MISSING: "年份待确认",
    UNDATED_NO_DATE: "原文未给日期",
    UNDATED_NOT_SET: "未设置日期",
}


# ---------- 画像字段白名单（数据模型 §4.4）----------
# 模型与前端都不得引入白名单外的字段：`family_income`、`id_card` 这类字段
# 一旦入库，就是"多采集了一份个人信息"，删库都不算挽回。
PROFILE_FIELDS = (
    "student_type", "grade", "gpa", "rank_percent", "disciplinary_record",
    "poverty_certified", "major", "credits_earned", "enrollment_status",
)

# 规则抽取里允许出现的 field_key（与上面同一白名单，单独命名便于阅读）
RULE_FIELD_KEYS = PROFILE_FIELDS


# ============================================================ M6：工作流与反馈
# ---------- 六节点（设计说明书 §7.6）----------
# 六个节点是**跨链路的总视图**，不是"每次提问都跑六个"。
# 一次问答真正走 intent → retrieval → analysis → validation；
# matching 与 planning 只在带画像 / 生成计划时执行，其余情况记为 skipped。
# 这一点必须显式建模：把没执行的节点画成绿色的"已完成"，比不画更糟。
NODE_INTENT = "intent"
NODE_RETRIEVAL = "retrieval"
NODE_ANALYSIS = "analysis"
NODE_MATCHING = "matching"
NODE_PLANNING = "planning"
NODE_VALIDATION = "validation"

NODE_ORDER = (
    NODE_INTENT, NODE_RETRIEVAL, NODE_ANALYSIS,
    NODE_MATCHING, NODE_PLANNING, NODE_VALIDATION,
)

NODE_LABELS = {
    NODE_INTENT: "意图识别",
    NODE_RETRIEVAL: "证据检索",
    NODE_ANALYSIS: "政策分析",
    NODE_MATCHING: "三态匹配",
    NODE_PLANNING: "计划生成",
    NODE_VALIDATION: "结果校验",
}

# ---------- 节点状态 ----------
NODE_OK = "succeeded"
NODE_DEGRADED = "degraded"
NODE_FAILED = "failed"
NODE_SKIPPED = "skipped"
NODE_STATUSES = (NODE_OK, NODE_DEGRADED, NODE_FAILED, NODE_SKIPPED)

# ---------- 运行状态（与设计 §7.6 的 workflow_runs.status 对齐）----------
RUN_RUNNING = "running"
RUN_SUCCEEDED = "succeeded"
RUN_DEGRADED = "degraded"
RUN_FAILED = "failed"
RUN_STATUSES = (RUN_RUNNING, RUN_SUCCEEDED, RUN_DEGRADED, RUN_FAILED)

RUN_TYPES = ("qa", "match", "plan")

# ---------- 运行结果 ----------
# status 说"这次跑完没有"，outcome 说"跑完是什么结果"。
# 拒答 = 跑成功了，只是正确答案是"不能答"——所以它必须能和 failed 区分开。
OUTCOME_ANSWERED = "answered"
OUTCOME_REFUSED = "refused"
OUTCOME_CITATION_REJECTED = "citation_rejected"
OUTCOME_DEGRADED = "degraded"
OUTCOME_FAILED = "failed"
OUTCOMES = (
    OUTCOME_ANSWERED, OUTCOME_REFUSED, OUTCOME_CITATION_REJECTED,
    OUTCOME_DEGRADED, OUTCOME_FAILED,
)

# ---------- 答案反馈 ----------
# 只收枚举码，不收自由文本当唯一信息：自由文本没法统计"哪一类问题最多"。
FEEDBACK_WRONG_FACT = "wrong_fact"
FEEDBACK_MISSING_CITATION = "missing_citation"
FEEDBACK_OUTDATED_POLICY = "outdated_policy"
FEEDBACK_TOO_VAGUE = "too_vague"
FEEDBACK_IRRELEVANT = "irrelevant"
FEEDBACK_OTHER = "other"
# R16：需求点名的五类（不相关 / 依据不准确 / 已过期 / 不完整 / 其他），
# 外加 M6 已有的「缺少引用」。不删 missing_citation——已有实现，删了会破坏
# 老数据与既有测试；「不相关」是需求新增，补齐。
FEEDBACK_REASONS = (
    FEEDBACK_WRONG_FACT, FEEDBACK_MISSING_CITATION, FEEDBACK_OUTDATED_POLICY,
    FEEDBACK_TOO_VAGUE, FEEDBACK_IRRELEVANT, FEEDBACK_OTHER,
)


# ---------- 反馈管理（R16） ----------
# 管理员处理状态三态：待处理 / 处理中 / 已解决
FEEDBACK_STATUS_PENDING = "pending"
FEEDBACK_STATUS_PROCESSING = "processing"
FEEDBACK_STATUS_RESOLVED = "resolved"
FEEDBACK_STATUSES = (
    FEEDBACK_STATUS_PENDING, FEEDBACK_STATUS_PROCESSING, FEEDBACK_STATUS_RESOLVED,
)

# 纠错反馈指向的对象类型
CORRECTION_TARGET_DOCUMENT = "document"
CORRECTION_TARGET_GUIDE = "guide"
CORRECTION_TARGET_MATCH = "match"
CORRECTION_TARGETS = (
    CORRECTION_TARGET_DOCUMENT, CORRECTION_TARGET_GUIDE, CORRECTION_TARGET_MATCH,
)
