"""ORM 模型：13 张表。

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


# 供 tools/init_db.py 与测试断言使用
ALL_MODELS = (
    User, Document, DocumentChunk, Conversation, Message, PolicyRule,
    MatchSession, MatchResultItem, ActionPlan, Task, WorkflowRun,
    QaFeedback, AuditLog,
)
