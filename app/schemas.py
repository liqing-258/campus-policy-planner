"""请求契约（pydantic v2）。

为什么单独一个文件：契约规范要求"请求体 extra='forbid'，未知字段直接 1001"。
把校验集中在这里，路由函数只接受已校验的数据，避免各处手写 if 判断。
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.errors import InvalidParam


class StrictModel(BaseModel):
    """统一开启未知字段拒绝。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class LoginRequest(StrictModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=72)


def validate(schema: type[StrictModel], payload: dict) -> BaseModel:
    """统一把 pydantic 的 ValidationError 翻译成 1001，并给出可读字段名。"""
    try:
        return schema.model_validate(payload)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(x) for x in first.get("loc", ())) or "请求体"
        reason = first.get("msg", "格式不正确")
        if first.get("type") == "extra_forbidden":
            reason = "不允许的字段"
        raise InvalidParam(f"参数校验失败：{loc} {reason}")


# ---------- 文档元数据字段 ----------
EFFECTIVE_STATUS_VALUES = ("active", "superseded", "expired", "unknown")
DOCUMENT_TEXT_FIELDS = {
    "title": 200,
    "department": 100,
    "academic_year": 16,
    "version_label": 32,
    "scope_note": 200,
}


def optional_text(raw: str | None, field: str) -> str | None:
    """上传表单里的可选文本字段：空串归一为 None，超长直接 1001。"""
    if raw is None:
        return None
    value = raw.strip()
    if value == "":
        return None
    limit = DOCUMENT_TEXT_FIELDS.get(field)
    if limit and len(value) > limit:
        raise InvalidParam(f"{field} 长度不能超过 {limit} 个字符")
    return value


def effective_status(raw: str | None) -> str:
    if raw is None or raw.strip() == "":
        return "unknown"
    value = raw.strip()
    if value not in EFFECTIVE_STATUS_VALUES:
        raise InvalidParam(
            "effective_status 必须是 " + "/".join(EFFECTIVE_STATUS_VALUES) + " 之一"
        )
    return value


def optional_date(raw: str | None, field: str = "published_at") -> date | None:
    if raw is None or raw.strip() == "":
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        raise InvalidParam(f"{field} 必须是 YYYY-MM-DD 格式")


# ---------- M4：检索与问答 ----------
class SearchRequest(StrictModel):
    """纯检索（调试/演示）。"""

    query: str = Field(min_length=1, max_length=300)
    top_k: int = Field(default=6, ge=1, le=20)
    document_ids: list[str] | None = None
    # 显式传 0 可临时关掉拒答闸门，仅用于调试对照——正常问答不传
    min_coverage: float | None = Field(default=None, ge=0.0, le=1.0)


class AskRequest(StrictModel):
    """提问。conversation_id 为空则新建会话；带 scope_document_ids 可限定只在这些文件里找。"""

    question: str = Field(min_length=1, max_length=500)
    conversation_id: str | None = Field(default=None, max_length=26)
    top_k: int = Field(default=6, ge=1, le=20)
    scope_document_ids: list[str] | None = None


class LlmAnswer(StrictModel):
    """**模型输出的契约**（N-01 契约校验闸就守在这里）。

    为什么用 pydantic 而不是手写 if：
    - 模型返回 `cited: "1,2"`（字符串）、`confidence: "HIGH"`（大写）、多给一个 `explanation` 字段，
      都属于常态。手写 if 只能挡住想得到的几种，pydantic 一次挡住全部形状问题。
    - `extra="forbid"` 是故意的：模型自作主张加字段时，宁可让它重试一次，
      也不要带着不确定字段往下走——下游引用校验依赖字段名精确。

    字段含义：
    - `answer`：给用户看的答案正文。**证据不足时应为空串**，并让 sufficient=false。
    - `cited`：引用的片段编号（1-based），必须是候选列表里真实存在的编号。
    - `sufficient`：模型自评证据是否足够。这是第二道拒答闸（第一道是覆盖率）。
    - `confidence`：提示性字段，不参与判定，只用于前端着色。
    """

    answer: str = Field(default="", max_length=4000)
    cited: list[int] = Field(default_factory=list, max_length=20)
    sufficient: bool = True
    confidence: str = Field(default="medium")

    @field_validator("confidence", mode="before")
    @classmethod
    def _normalize_confidence(cls, value):
        """大小写与非法值都归一，不让它把整条回答废掉——它只是提示字段。"""
        if not isinstance(value, str):
            return "medium"
        lowered = value.strip().lower()
        return lowered if lowered in ("high", "medium", "low") else "medium"

    @field_validator("cited", mode="before")
    @classmethod
    def _drop_booleans(cls, value):
        """`cited: [true]` 会被 pydantic 宽松模式转成 `[1]`，从而**误指到片段 1**。

        这类"看起来合法的错误引用"比明显的越界更危险：它通得过类型校验，
        用户却会看到一条根本不对应问题的出处。因此在进入校验前先滤掉布尔值。
        """
        if isinstance(value, list):
            return [v for v in value if not isinstance(v, bool)]
        return value


# ============================================================ M5：规则 / 匹配 / 计划 / 任务
def _check_in(value: str | None, allowed: tuple[str, ...], label: str) -> str | None:
    """把"取值不在白名单"翻译成 1001，而不是让 pydantic 报一个用户看不懂的类型错误。

    用校验器而不是 Literal：白名单是 shared/enums.py 里的一份定义，
    用 Literal 就得在这里再抄一遍——两份清单早晚会不一致。
    """
    if value is None:
        return value
    if value not in allowed:
        raise ValueError(f"{label}必须是 {'/'.join(allowed)} 之一")
    return value


class ExtractRequest(StrictModel):
    """规则抽取入参。不传 chunk_ids 表示整篇抽取。"""

    chunk_ids: list[str] | None = Field(default=None, max_length=200)
    max_rules_per_chunk: int = Field(default=8, ge=1, le=20)


class ExtractedRule(StrictModel):
    """模型输出的一条规则。**字段类型全部宽松**，逐条合法性由服务层判定。

    为什么不在这里用 Literal 卡死 operator：一条规则算子写错，应该丢掉这一条并告诉
    用户"第 3 条算子不认识"，而不是让整次抽取因为一条脏数据全盘失败。
    """

    chunk_index: int
    rule_code: str = Field(min_length=1, max_length=32)
    rule_type: str = Field(min_length=1, max_length=24)
    description: str = Field(min_length=1, max_length=500)
    field_key: str | None = Field(default=None, max_length=64)
    operator: str | None = Field(default=None, max_length=20)
    expected_value: str | None = Field(default=None, max_length=200)
    target_group: str | None = Field(default=None, max_length=64)
    required_information: str | None = Field(default=None, max_length=500)
    effective_period: str | None = Field(default=None, max_length=64)
    page_or_section: str | None = Field(default=None, max_length=100)
    original_quote: str = Field(min_length=1, max_length=1000)
    confidence: str | None = Field(default=None, max_length=12)


class RuleExtractionPayload(StrictModel):
    rules: list[ExtractedRule] = Field(default_factory=list)


class RulePatch(StrictModel):
    """管理员修正规则。全部可选，至少给一个字段。"""

    rule_type: str | None = None
    description: str | None = Field(default=None, max_length=500)
    field_key: str | None = None
    operator: str | None = None
    expected_value: str | None = Field(default=None, max_length=200)
    target_group: str | None = Field(default=None, max_length=64)
    required_information: str | None = Field(default=None, max_length=500)
    effective_period: str | None = Field(default=None, max_length=64)
    review_status: str | None = None

    @field_validator("rule_type")
    @classmethod
    def _rule_type(cls, value):
        from shared.enums import RULE_TYPES
        return _check_in(value, RULE_TYPES, "规则类型")

    @field_validator("operator")
    @classmethod
    def _operator(cls, value):
        from shared.enums import RULE_OPERATORS
        return _check_in(value, RULE_OPERATORS, "算子")

    @field_validator("field_key")
    @classmethod
    def _field_key(cls, value):
        from shared.enums import RULE_FIELD_KEYS
        return _check_in(value, RULE_FIELD_KEYS, "画像字段")

    @field_validator("review_status")
    @classmethod
    def _review_status(cls, value):
        from shared.enums import REVIEW_STATUSES
        return _check_in(value, REVIEW_STATUSES, "复核状态")


class Profile(StrictModel):
    """学生画像。**字段全部可选**：缺字段是正常情况（对应 unknown/missing_info），

    不是错误——逼用户填齐所有字段才给匹配，等于把"信息不足"这个真实场景藏起来。
    白名单外字段一律 1001：见 shared/enums.py 里 PROFILE_FIELDS 的注释。
    """

    student_type: str | None = Field(default=None, max_length=64)
    grade: int | None = Field(default=None, ge=1, le=8)
    gpa: float | None = Field(default=None, ge=0, le=5)
    rank_percent: float | None = Field(default=None, ge=0, le=100)
    disciplinary_record: str | None = Field(default=None, max_length=64)
    poverty_certified: str | None = Field(default=None, max_length=16)
    major: str | None = Field(default=None, max_length=128)
    credits_earned: int | None = Field(default=None, ge=0, le=500)
    enrollment_status: str | None = Field(default=None, max_length=32)


class MatchRequest(StrictModel):
    document_id: str = Field(min_length=1, max_length=26)
    profile: Profile = Field(default_factory=Profile)


class PlanDraftRequest(StrictModel):
    document_id: str = Field(min_length=1, max_length=26)
    match_session_id: str | None = Field(default=None, max_length=26)


class DraftTaskInput(StrictModel):
    """确认计划时前端回传的任务。临时 ID 只在本次请求内有效。

    `source_rule_code` / `due_source` / `note` / `needs_human_confirmation` 是草稿
    自带的溯源元数据，原样回存。它们只影响"这条任务从哪来、为什么缺日期"的展示，
    不参与任何资格判定——改它们不会让不符合条件的人通过匹配。
    """

    temp_id: str = Field(min_length=1, max_length=8)
    title: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    priority: str = "P2"
    due_at: str | None = None
    required_materials: str | None = Field(default=None, max_length=1000)
    dependency_ids: list[str] = Field(default_factory=list, max_length=50)
    source_rule_code: str | None = Field(default=None, max_length=32)
    due_source: str | None = Field(default=None, max_length=12)
    note: str | None = Field(default=None, max_length=500)
    needs_human_confirmation: bool = False

    @field_validator("priority")
    @classmethod
    def _priority(cls, value):
        from shared.enums import PRIORITIES
        return _check_in(value, PRIORITIES, "优先级")

    @field_validator("due_source")
    @classmethod
    def _due_source(cls, value):
        return _check_in(value, ("rule", "manual"), "日期来源")


class PlanConfirmRequest(StrictModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    tasks: list[DraftTaskInput] = Field(default_factory=list, max_length=50)


class PlanPatchRequest(StrictModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    status: str | None = None

    @field_validator("status")
    @classmethod
    def _status(cls, value):
        from shared.enums import PLAN_STATUSES
        return _check_in(value, PLAN_STATUSES, "计划状态")


class TaskCreateRequest(StrictModel):
    plan_id: str = Field(min_length=1, max_length=26)
    title: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    priority: str = "P2"
    due_at: str | None = None
    required_materials: str | None = Field(default=None, max_length=1000)

    @field_validator("priority")
    @classmethod
    def _priority(cls, value):
        from shared.enums import PRIORITIES
        return _check_in(value, PRIORITIES, "优先级")


class TaskPatchRequest(StrictModel):
    title: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    priority: str | None = None
    due_at: str | None = None
    required_materials: str | None = Field(default=None, max_length=1000)

    @field_validator("priority")
    @classmethod
    def _priority(cls, value):
        from shared.enums import PRIORITIES
        return _check_in(value, PRIORITIES, "优先级")


class TaskStatusRequest(StrictModel):
    status: str

    @field_validator("status")
    @classmethod
    def _status(cls, value):
        from shared.enums import TASK_STATUSES
        return _check_in(value, TASK_STATUSES, "任务状态")
