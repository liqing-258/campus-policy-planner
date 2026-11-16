"""ORM 模型：17 张表。

字段与索引的唯一权威来源是 docs/20-系统设计/数据模型.md。
本文件不写业务逻辑，只做结构映射；改字段前先改文档。

约定：
- 主键统一 26 位 ULID（与 request_id 同一生成器）
- 时间统一用 naive UTC 存储，出参再格式化为 ISO 8601 + Z
- 枚举存字符串字面值，校验在 shared/enums.py
"""

from __future__ import annotations

from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from sqlalchemy.engine import Engine

db = SQLAlchemy()


def utcnow() -> datetime:
    """DB 内统一存 naive UTC，避免 SQLite 无时区导致比较错乱。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_connection, _record) -> None:
    """SQLite 三项必设 PRAGMA（数据模型文档 §5）。

    - journal_mode=WAL：读写并发不互相阻塞
    - foreign_keys=ON：SQLite 默认不校验外键，不设等于约束失效
    - busy_timeout：并发写入时等待而非立即报 database is locked
    """
    module = type(dbapi_connection).__module__
    if "sqlite" not in module.lower():
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


class TimestampMixin:
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


# ---------------------------------------------------------------- 3.1 users
class User(db.Model):
    __tablename__ = "users"
    __table_args__ = (
        db.UniqueConstraint("username", name="uq_users_username"),
    )

    id = db.Column(db.String(26), primary_key=True)
    username = db.Column(db.String(64), nullable=False)
    display_name = db.Column(db.String(64), nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)
    role = db.Column(db.String(16), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    def to_public(self) -> dict:
        return {
            "id": self.id,
            "username": self.username,
            "display_name": self.display_name,
            "role": self.role,
        }


# ------------------------------------------------------------ 3.2 documents
class Document(db.Model):
    __tablename__ = "documents"
    __table_args__ = (
        db.UniqueConstraint("stored_name", name="uq_documents_stored_name"),
        db.Index("idx_documents_parse_status", "parse_status"),
        db.Index("idx_documents_effective_status", "effective_status"),
        db.Index("idx_documents_hash", "file_hash"),
        db.Index("idx_documents_academic_year", "academic_year"),
    )

    id = db.Column(db.String(26), primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    file_name = db.Column(db.String(255), nullable=False)
    stored_name = db.Column(db.String(64), nullable=False)
    file_type = db.Column(db.String(16), nullable=False)
    file_size = db.Column(db.Integer, nullable=False)
    file_hash = db.Column(db.String(64))
    department = db.Column(db.String(100))
    published_at = db.Column(db.Date)
    academic_year = db.Column(db.String(16))
    version_label = db.Column(db.String(32))
    effective_status = db.Column(db.String(16), nullable=False, default="unknown")
    supersedes_id = db.Column(db.String(26), db.ForeignKey("documents.id"))
    scope_note = db.Column(db.String(200))
    parse_status = db.Column(db.String(16), nullable=False, default="uploaded")
    parse_error = db.Column(db.Text)
    chunk_count = db.Column(db.Integer, nullable=False, default=0)
    uploader_id = db.Column(db.String(26), db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)
    deleted_at = db.Column(db.DateTime)

    chunks = db.relationship(
        "DocumentChunk",
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


# ------------------------------------------------------ 3.3 document_chunks
class DocumentChunk(db.Model):
    __tablename__ = "document_chunks"
    __table_args__ = (
        db.UniqueConstraint("document_id", "chunk_index", name="uq_chunks_doc_index"),
        db.Index("idx_chunks_document", "document_id"),
    )

    id = db.Column(db.String(26), primary_key=True)
    document_id = db.Column(
        db.String(26), db.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    chunk_index = db.Column(db.Integer, nullable=False)
    content = db.Column(db.Text, nullable=False)
    char_start = db.Column(db.Integer, nullable=False)
    char_end = db.Column(db.Integer, nullable=False)
    page_number = db.Column(db.Integer)
    section_title = db.Column(db.String(200))
    token_count = db.Column(db.Integer)
    embedding = db.Column(db.LargeBinary)
    embedding_model = db.Column(db.String(64))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    document = db.relationship("Document", back_populates="chunks")


# --------------------------------------------------------- 3.4 conversations
class Conversation(db.Model):
    __tablename__ = "conversations"
    __table_args__ = (
        db.Index("idx_conv_user_created", "user_id", "created_at"),
    )

    id = db.Column(db.String(26), primary_key=True)
    user_id = db.Column(db.String(26), db.ForeignKey("users.id"), nullable=False)
    title = db.Column(db.String(120))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


# -------------------------------------------------------------- 3.5 messages
class Message(db.Model):
    __tablename__ = "messages"
    __table_args__ = (
        db.Index("idx_msg_conv_created", "conversation_id", "created_at"),
    )

    id = db.Column(db.String(26), primary_key=True)
    conversation_id = db.Column(
        db.String(26), db.ForeignKey("conversations.id"), nullable=False
    )
    role = db.Column(db.String(12), nullable=False)
    content = db.Column(db.Text, nullable=False)
    answer_json = db.Column(db.Text)
    citations_json = db.Column(db.Text)
    workflow_run_id = db.Column(db.String(26), db.ForeignKey("workflow_runs.id"))
    model_name = db.Column(db.String(64))
    latency_ms = db.Column(db.Integer)
    mode = db.Column(db.String(16))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


# ---------------------------------------------------------- 3.6 policy_rules
class PolicyRule(db.Model):
    __tablename__ = "policy_rules"
    __table_args__ = (
        db.Index("idx_rules_document", "document_id"),
        db.Index("idx_rules_chunk", "chunk_id"),
        db.Index("idx_rules_review", "review_status"),
    )

    id = db.Column(db.String(26), primary_key=True)
    document_id = db.Column(db.String(26), db.ForeignKey("documents.id"), nullable=False)
    chunk_id = db.Column(db.String(26), db.ForeignKey("document_chunks.id"), nullable=False)
    rule_code = db.Column(db.String(32), nullable=False)
    rule_type = db.Column(db.String(24), nullable=False)
    description = db.Column(db.Text, nullable=False)
    target_group = db.Column(db.String(64))
    field_key = db.Column(db.String(64))
    operator = db.Column(db.String(20))
    expected_value = db.Column(db.Text)
    required_information = db.Column(db.Text)
    effective_period = db.Column(db.String(64))
    page_or_section = db.Column(db.String(100))
    original_quote = db.Column(db.Text, nullable=False)
    quote_verified = db.Column(db.Integer, nullable=False, default=0)
    extraction_status = db.Column(db.String(16), nullable=False, default="suggested")
    review_status = db.Column(db.String(16), nullable=False, default="needs_review")
    confidence = db.Column(db.String(12))
    schema_version = db.Column(db.String(8), nullable=False, default="v1")
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


# -------------------------------------------------------- 3.7 match_sessions
class MatchSession(db.Model):
    __tablename__ = "match_sessions"
    __table_args__ = (
        db.Index("idx_ms_user_doc", "user_id", "document_id"),
        db.Index("idx_ms_created", "created_at"),
    )

    id = db.Column(db.String(26), primary_key=True)
    user_id = db.Column(db.String(26), db.ForeignKey("users.id"), nullable=False)
    document_id = db.Column(db.String(26), db.ForeignKey("documents.id"), nullable=False)
    profile_json = db.Column(db.Text, nullable=False)
    rule_snapshot_json = db.Column(db.Text, nullable=False)
    sum_met = db.Column(db.Integer, nullable=False, default=0)
    sum_not_met = db.Column(db.Integer, nullable=False, default=0)
    sum_unknown = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


# ---------------------------------------------------- 3.8 match_result_items
class MatchResultItem(db.Model):
    __tablename__ = "match_result_items"
    __table_args__ = (
        db.Index("idx_mri_session", "session_id"),
        db.Index("idx_mri_status", "status"),
    )

    id = db.Column(db.String(26), primary_key=True)
    session_id = db.Column(
        db.String(26), db.ForeignKey("match_sessions.id"), nullable=False
    )
    rule_id = db.Column(db.String(26), db.ForeignKey("policy_rules.id"), nullable=False)
    status = db.Column(db.String(12), nullable=False)
    unknown_reason = db.Column(db.String(24))
    user_value = db.Column(db.String(200))
    expected_value = db.Column(db.String(200))
    rationale = db.Column(db.Text)
    evidence_chunk_id = db.Column(db.String(26), db.ForeignKey("document_chunks.id"))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


# --------------------------------------------------------- 3.9 action_plans
class ActionPlan(db.Model):
    __tablename__ = "action_plans"
    __table_args__ = (
        db.Index("idx_plan_user_status", "user_id", "status"),
    )

    id = db.Column(db.String(26), primary_key=True)
    user_id = db.Column(db.String(26), db.ForeignKey("users.id"), nullable=False)
    document_id = db.Column(db.String(26), db.ForeignKey("documents.id"), nullable=False)
    match_session_id = db.Column(db.String(26), db.ForeignKey("match_sessions.id"))
    title = db.Column(db.String(200), nullable=False)
    status = db.Column(db.String(16), nullable=False, default="draft")
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    confirmed_at = db.Column(db.DateTime)


# ----------------------------------------------------------------- 3.10 tasks
class Task(db.Model):
    __tablename__ = "tasks"
    __table_args__ = (
        db.Index("idx_task_user_status", "user_id", "status"),
        db.Index("idx_task_due", "due_at"),
        db.Index("idx_task_plan", "plan_id"),
    )

    id = db.Column(db.String(26), primary_key=True)
    plan_id = db.Column(db.String(26), db.ForeignKey("action_plans.id"), nullable=False)
    user_id = db.Column(db.String(26), db.ForeignKey("users.id"), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    required_materials = db.Column(db.Text)
    due_at = db.Column(db.DateTime)
    due_source = db.Column(db.String(12))
    priority = db.Column(db.String(4), nullable=False, default="P2")
    status = db.Column(db.String(12), nullable=False, default="todo")
    source_document_id = db.Column(db.String(26), db.ForeignKey("documents.id"))
    source_rule_id = db.Column(db.String(26), db.ForeignKey("policy_rules.id"))
    needs_human_confirmation = db.Column(db.Integer, nullable=False, default=0)
    dependency_ids = db.Column(db.Text)
    # 草稿里那句「为什么截止日期是空的 / 需要人工确认什么」必须跟着任务走。
    # 没有这一列时，确认后的任务只剩 due_at=NULL，前端只好写死「原文未给出日期」——
    # 而真实政策写的是「当年 9 月 10 日」，只是缺年份，那句写死的提示是错的。
    note = db.Column(db.String(500))
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)
    completed_at = db.Column(db.DateTime)


# ---------------------------------------------------------- 3.11 workflow_runs
class WorkflowRun(db.Model):
    __tablename__ = "workflow_runs"
    __table_args__ = (
        db.Index("idx_wf_user_created", "user_id", "created_at"),
        db.Index("idx_wf_status", "status"),
    )

    id = db.Column(db.String(26), primary_key=True)
    user_id = db.Column(db.String(26), db.ForeignKey("users.id"), nullable=False)
    run_type = db.Column(db.String(16), nullable=False)
    status = db.Column(db.String(16), nullable=False)
    node_traces_json = db.Column(db.Text)
    total_ms = db.Column(db.Integer)
    model_calls = db.Column(db.Integer, nullable=False, default=0)
    error = db.Column(db.Text)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


# ----------------------------------------------------------- 3.12 qa_feedback
class QaFeedback(db.Model):
    __tablename__ = "qa_feedback"
    __table_args__ = (
        db.Index("idx_feedback_message", "message_id"),
        db.Index("idx_feedback_created", "created_at"),
    )

    id = db.Column(db.String(26), primary_key=True)
    message_id = db.Column(db.String(26), db.ForeignKey("messages.id"), nullable=False)
    user_id = db.Column(db.String(26), db.ForeignKey("users.id"), nullable=False)
    helpful = db.Column(db.Integer, nullable=False)
    reason_code = db.Column(db.String(20))
    comment = db.Column(db.Text)
    # R16：管理员处理状态。**老库加列**由 `ensure_columns()` 做幂等迁移——
    # create_all() 不改已存在的表，这三列不在迁移里加，老库一查询就 no such column。
    status = db.Column(db.String(16), nullable=False, default="pending")
    admin_note = db.Column(db.Text)
    resolved_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


# ------------------------------------------------- 3.12b correction_feedback
class CorrectionFeedback(db.Model):
    """纠错反馈（R16）：对政策文件 / 指南 / 匹配结果内容的纠错。

    **为什么不用 qa_feedback**：qa_feedback 的 `message_id` 是 NOT NULL 外键，
    而"这份政策的发布日期写错了"不指向任何一条 AI 消息。所以纠错反馈是另一张表，
    用 `target_type + target_id` 定位被纠错的对象，`document_id` 关联到政策文件
    （统计"政策错误反馈"与管理员跳转都靠它）。

    **学生反馈不碰正式政策内容**：这张表只记录"谁、对什么、说了什么"，管理员
    看完之后走既有的文档元数据接口 / 规则复核流程去改，本表没有任何写 documents /
    policy_rules 的副作用——"未经核实的用户意见不得自动成为政策规则"由此成立。
    """

    __tablename__ = "correction_feedback"
    __table_args__ = (
        db.Index("idx_correction_target", "target_type", "target_id"),
        db.Index("idx_correction_created", "created_at"),
    )

    id = db.Column(db.String(26), primary_key=True)
    user_id = db.Column(db.String(26), db.ForeignKey("users.id"), nullable=False)
    # document = 政策详情；guide = 校园事务指南事项；match = 资格匹配结果
    target_type = db.Column(db.String(16), nullable=False)
    target_id = db.Column(db.String(64), nullable=False)
    document_id = db.Column(db.String(26), db.ForeignKey("documents.id"))
    reason_code = db.Column(db.String(20))
    comment = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(16), nullable=False, default="pending")
    admin_note = db.Column(db.Text)
    resolved_by = db.Column(db.String(26), db.ForeignKey("users.id"))
    resolved_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


# ------------------------------------------------------------ 3.13 audit_logs
class AuditLog(db.Model):
    __tablename__ = "audit_logs"
    __table_args__ = (
        db.Index("idx_audit_created", "created_at"),
        db.Index("idx_audit_actor", "user_id", "created_at"),
        db.Index("idx_audit_resource", "resource_type", "resource_id"),
    )

    id = db.Column(db.String(26), primary_key=True)
    user_id = db.Column(db.String(26), db.ForeignKey("users.id"), nullable=False)
    action = db.Column(db.String(48), nullable=False)
    resource_type = db.Column(db.String(24), nullable=False)
    resource_id = db.Column(db.String(26))
    detail_json = db.Column(db.Text)
    request_id = db.Column(db.String(36))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


# ------------------------------------------------------- 3.14 calendar_events
class CalendarEvent(db.Model):
    """学生自己添加的日程（R14 校园日历）。

    **为什么不复用 tasks**

    `tasks.plan_id` 是 `NOT NULL` 外键，而"周三去图书馆还书"不属于任何一份
    政策计划。两条绕开的路都不通：

    1. 给 tasks 加列 / 放开 NOT NULL —— 项目用 `db.create_all()` 建表，
       老库不会被自动改结构；加列会让已经升级过的库启动即报 `no such column`，
       改约束要重建整张表（12 步），风险远超收益。
    2. 为个人事务伪造一条 action_plan 与一份 document —— `PRAGMA foreign_keys=ON`
       是真的在生效，伪造的 document_id 要么外键失败，要么在知识库里多出一份
       不存在的"政策"，污染检索与推荐。

    所以个人日程单独一张表。它与任务在**接口层**合并成一个日程流，
    前端只认一种形状；`kind` 字段说明它来自哪一边。

    日期用 `db.Date` 而不是 `db.DateTime`：日程是"哪一天"，
    存成 DateTime 反而要处理时区与"00:00 属于哪一天"的问题。
    """

    __tablename__ = "calendar_events"
    __table_args__ = (
        db.Index("idx_event_user_date", "user_id", "event_date"),
    )

    id = db.Column(db.String(26), primary_key=True)
    user_id = db.Column(db.String(26), db.ForeignKey("users.id"), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    note = db.Column(db.String(500))
    event_date = db.Column(db.Date, nullable=False)
    # "HH:MM"，可空。为空表示"这一天，具体时间未定"，前端不显示时间
    event_time = db.Column(db.String(5))
    category = db.Column(db.String(24), nullable=False, default="personal")
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)


# ------------------------------------------------------- 3.15 favorites
class Favorite(db.Model):
    """常用政策收藏（R17）。

    **为什么是一张独立的关联表，而不是在各处加「收藏」布尔列**

    收藏对象横跨四类既有实体（政策文件 / 指南事项 / 问答 / 事务计划），
    给每一张表都加一列「收藏状态」意味着改 4 张表 + 4 份迁移，且「谁收藏了」
    本身就是按用户隔离的多对多关系，塞进目标表反而说不清归属。一张
    `user_id + target_type + target_id` 的关联表最直白，也满足「不改动原有
    政策文件和问答的数据结构」这条硬约束。

    **为什么存 title / document_id 快照**

    - `title`：目标被下架 / 删除后，收藏列表仍要能显示「收藏的是什么」；
    - `document_id`：把「关联政策文件」这一维固定下来，展示时实时 JOIN 拿
      `effective_status`，于是「政策更新或失效后同步显示最新有效状态」成立——
      快照存的是**指向**，不是**状态**，状态永远现算。

    同一个人对同一个目标只留一条：唯一约束 `user + target_type + target_id`。
    重复收藏是覆盖（幂等），不是新增——和 qa_feedback 的幂等方式一致。
    """

    __tablename__ = "favorites"
    __table_args__ = (
        db.UniqueConstraint("user_id", "target_type", "target_id",
                            name="uq_favorites_user_target"),
        db.Index("idx_favorites_user_created", "user_id", "created_at"),
        db.Index("idx_favorites_target", "target_type", "target_id"),
    )

    id = db.Column(db.String(26), primary_key=True)
    user_id = db.Column(db.String(26), db.ForeignKey("users.id"), nullable=False)
    # document=政策文件 / guide=指南事项 / qa=问答 / plan=事务计划
    target_type = db.Column(db.String(16), nullable=False)
    target_id = db.Column(db.String(64), nullable=False)
    # 收藏那一刻的名称快照；目标改名/删除后仍能显示
    title = db.Column(db.String(200), nullable=False)
    # 关联政策文件（document 类型即其自身；guide/qa/plan 收藏时解析出的政策）
    document_id = db.Column(db.String(26), db.ForeignKey("documents.id"))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


# --------------------------------------------------- 3.16 document_rule_states（文档 §3.17）
class DocumentRuleState(db.Model):
    """文件级的「资格匹配准备度」记录（R20）。

    **为什么不给 documents 加列**

    这个状态包含两件**只能靠人/事件留下**的事实：

    1. 管理员判定「这份政策不适用于资格匹配」+ 理由（食堂指南、违纪处分办法
       这类文件本来就没有申请资格条件，强行抽只会抽出流程类规则，让学生误以为
       自己"不符合"）；
    2. 上一次抽取尝试的结果（失败原因必须留下来，否则"抽取失败"和"从没抽过"
       在界面上完全一样——这正是 R20 的现场）。

    加列要 ALTER 四张文档表的既有结构；项目 `db.create_all()` 只建缺失的表，
    新表零迁移就能落地，且完全不动既有表——符合「不改动原有数据结构」的约束。
    一对一共存不是浪费：**它是"事件与决定"的家，不是文档元数据**。

    其余状态（已具备/待审核/待提取）一律**推导**，不落库：它们随规则表变化，
    落库会在管理员复核后静默过期，而它没有"过期"标记。
    """

    __tablename__ = "document_rule_states"
    __table_args__ = (
        db.UniqueConstraint("document_id", name="uq_rule_state_document"),
        db.Index("idx_rule_state_applicability", "applicability"),
    )

    id = db.Column(db.String(26), primary_key=True)
    document_id = db.Column(db.String(26), db.ForeignKey("documents.id"), nullable=False)
    # unclassified / applicable / not_applicable
    applicability = db.Column(db.String(20), nullable=False, default="unclassified")
    # 「不适用」的理由：给下一次看这份文件的人解释，不是给机器读
    applicability_note = db.Column(db.String(500))
    decided_by = db.Column(db.String(26), db.ForeignKey("users.id"))
    decided_at = db.Column(db.DateTime)
    # 上一次抽取尝试
    last_attempt_at = db.Column(db.DateTime)
    last_attempt_status = db.Column(db.String(16))  # succeeded / failed
    last_attempt_message = db.Column(db.Text)
    # 那次抽取落了多少条（含被丢弃的行数另记在 message 里，避免再开列）
    last_attempt_rules = db.Column(db.Integer)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)


# 供 tools/init_db.py 与测试断言使用
ALL_MODELS = (
    User, Document, DocumentChunk, Conversation, Message, PolicyRule,
    MatchSession, MatchResultItem, ActionPlan, Task, WorkflowRun,
    QaFeedback, CorrectionFeedback, AuditLog, CalendarEvent, Favorite,
    DocumentRuleState,
)


# ------------------------------------------------------------ 轻量迁移（R16）
def ensure_columns() -> list[str]:
    """给老表幂等加列。create_all() 只建缺失的表、不改已存在的表，
    所以 qa_feedback 的 R16 三列（status/admin_note/resolved_at）要在这里补。

    用 `ALTER TABLE ... ADD COLUMN` 是因为 SQLite 对它完全支持且不碰既有行；
    R14 拒绝给 tasks 加列是因为那是「改 NOT NULL 约束」要重建整张表，跟这里
    不是一回事。返回实际加上的列名列表（空 = 已经是最新结构）。

    调用方：`bootstrap.ensure_schema()`（演示/打包自动）与 `tools/init_db.py`
    （开发手动），两个入口都调，保证任何启动路径都拿到新列。
    """
    from sqlalchemy import inspect, text

    added: list[str] = []
    inspector = inspect(db.engine)
    if "qa_feedback" not in inspector.get_table_names():
        return added

    existing = {col["name"] for col in inspector.get_columns("qa_feedback")}
    specs = (
        ("status", "VARCHAR(16) NOT NULL DEFAULT 'pending'"),
        ("admin_note", "TEXT"),
        ("resolved_at", "DATETIME"),
    )
    for name, ddl in specs:
        if name not in existing:
            db.session.execute(text(f"ALTER TABLE qa_feedback ADD COLUMN {name} {ddl}"))
            added.append(name)
    if added:
        db.session.commit()
    return added
