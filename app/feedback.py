"""办事评价与纠错反馈（R16）。

## 两条反馈，两条表，一个统一视图

- **答案评价**（有用/没用）：落在 M6 就有的 `qa_feedback`，提交接口是
  `POST /messages/{id}/feedback`（本模块不动它，只在这里读出来给管理员看）。
- **纠错反馈**（政策详情 / 指南 / 匹配结果的"这里写错了"）：落在本轮的
  `correction_feedback`，因为 `qa_feedback.message_id` 是 NOT NULL 外键，
  而"这份政策的发布日期写错了"不指向任何一条 AI 消息。

管理员看到的是一份**合并视图**：两类行都带 `kind`，公共字段（用户 / 原因 /
正文 / 处理状态 / 时间）同形，差异只在 `context`（答案评价带问题与 AI 回答，
纠错反馈带目标对象与政策文件）。

## 学生反馈不能改正式政策内容

`correction_feedback` 只记录"谁、对什么、说了什么"。管理员处理后走既有的
文档元数据接口 / 规则复核流程去改，本模块没有任何写 documents / policy_rules
的副作用——"未经核实的用户意见不得自动成为政策规则"由此成立，而非靠话术。

## 统计口径

| 指标 | 算法 |
| --- | --- |
| 有帮助比例 | helpful 的 qa_feedback ÷ qa_feedback 总数 |
| 无帮助比例 | 1 − 有帮助比例（qa_feedback 内部） |
| 政策错误反馈 | reason_code ∈ {wrong_fact, outdated_policy} 的答案评价 + 全部纠错反馈 |
| 高频问题类型 | reason_code 分布（含 missing_citation / too_vague / irrelevant / other） |
| 已解决 | status = resolved 的两类行 |
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from flask import Blueprint, request

from app.auth import admin_required, current_user, login_required
from app.contract import new_ulid, ok, page_args, paginated
from app.errors import FeedbackNotFound, InvalidParam
from app.models import (
    AuditLog,
    CorrectionFeedback,
    Document,
    Message,
    QaFeedback,
    User,
    db,
    utcnow,
)
from app.schemas import (
    CorrectionFeedbackRequest,
    FeedbackAdminUpdateRequest,
    validate,
)
from shared.enums import (
    FEEDBACK_OUTDATED_POLICY,
    FEEDBACK_STATUSES,
    FEEDBACK_WRONG_FACT,
)

bp = Blueprint("feedback", __name__)

# 单次聚合读取上限，与 analytics 同源：防库被塞爆后管理页转圈。
MAX_ROWS = 2000

# 政策错误反馈的判定码：依据不准确 / 已过期。这两个直接指向"政策内容有问题"，
# 与"回答不相关 / 太笼统 / 缺引用"这类"模型回答质量问题"分开统计。
_POLICY_ERROR_REASONS = (FEEDBACK_WRONG_FACT, FEEDBACK_OUTDATED_POLICY)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _audit(action: str, resource_type: str, resource_id: str | None,
           detail: str | None = None) -> None:
    """审计只追加，失败不阻断主流程（与 documents._audit 同源）。"""
    try:
        db.session.add(AuditLog(
            id=new_ulid(),
            user_id=current_user().id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            detail_json=detail,
            request_id=request.headers.get("X-Request-Id"),
        ))
    except Exception:
        pass


def _user_brief(user: User | None) -> dict:
    if user is None:
        return {"id": None, "display_name": "已删除用户", "username": None}
    return {"id": user.id, "display_name": user.display_name, "username": user.username}


def _citations_of(message: Message | None) -> list[dict]:
    if message is None or not message.citations_json:
        return []
    try:
        data = json.loads(message.citations_json)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def _question_of(message: Message | None) -> str | None:
    """同会话里、该回答之前最近的一条用户提问。"""
    if message is None:
        return None
    row = db.session.execute(
        db.select(Message)
        .where(
            Message.conversation_id == message.conversation_id,
            Message.role == "user",
            Message.created_at <= message.created_at,
        )
        .order_by(Message.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return row.content if row is not None else None


def _serialize_qa(fb: QaFeedback, user: User | None) -> dict:
    message = db.session.get(Message, fb.message_id)
    return {
        "kind": "qa",
        "id": fb.id,
        "user": _user_brief(user),
        "helpful": bool(fb.helpful),
        "reason_code": fb.reason_code,
        "comment": fb.comment,
        "status": fb.status or "pending",
        "admin_note": fb.admin_note,
        "created_at": _iso(fb.created_at),
        "resolved_at": _iso(fb.resolved_at),
        "context": {
            "question": _question_of(message),
            "answer": message.content if message is not None else None,
            "model": message.model_name if message is not None else None,
            "conversation_id": message.conversation_id if message is not None else None,
            "citations": _citations_of(message),
        },
    }


def _serialize_correction(fb: CorrectionFeedback, user: User | None) -> dict:
    document = db.session.get(Document, fb.document_id) if fb.document_id else None
    return {
        "kind": "correction",
        "id": fb.id,
        "user": _user_brief(user),
        "helpful": None,
        "reason_code": fb.reason_code,
        "comment": fb.comment,
        "status": fb.status or "pending",
        "admin_note": fb.admin_note,
        "created_at": _iso(fb.created_at),
        "resolved_at": _iso(fb.resolved_at),
        "context": {
            "target_type": fb.target_type,
            "target_id": fb.target_id,
            "document": (
                {
                    "id": document.id,
                    "title": document.title,
                    "effective_status": document.effective_status,
                    "department": document.department,
                    "academic_year": document.academic_year,
                    "version_label": document.version_label,
                }
                if document is not None else None
            ),
        },
    }


# ============================================================ 学生端：纠错反馈
@bp.post("/feedback/corrections")
@login_required
def submit_correction():
    """提交纠错反馈。同一个人对同一对象重复提交是**覆盖**，不是新增——
    和答案评价的幂等方式一致：重复点"提交"不该刷出一排一模一样的记录。"""
    body: CorrectionFeedbackRequest = validate(
        CorrectionFeedbackRequest, request.get_json(silent=True) or {})
    user = current_user()

    existing = db.session.execute(
        db.select(CorrectionFeedback).where(
            CorrectionFeedback.user_id == user.id,
            CorrectionFeedback.target_type == body.target_type,
            CorrectionFeedback.target_id == body.target_id,
        )
    ).scalar_one_or_none()

    updated = existing is not None
    if existing is None:
        row = CorrectionFeedback(
            id=new_ulid(), user_id=user.id,
            target_type=body.target_type, target_id=body.target_id,
            document_id=body.document_id, reason_code=body.reason_code,
            comment=body.comment, status="pending",
        )
        db.session.add(row)
    else:
        row = existing
        row.document_id = body.document_id
        row.reason_code = body.reason_code
        row.comment = body.comment
        # 重新提交 = 重新提给管理员看，状态回到待处理，清掉上一次的处理说明
        row.status = "pending"
        row.admin_note = None
        row.resolved_at = None
        row.resolved_by = None
    _audit("feedback.correction", "correction_feedback", row.id,
           json.dumps({"target_type": body.target_type, "target_id": body.target_id}))
    db.session.commit()

    return ok({
        "feedback_id": row.id,
        "target_type": row.target_type,
        "target_id": row.target_id,
        "comment": row.comment,
        "reason_code": row.reason_code,
        "updated": updated,
        "created_at": _iso(row.created_at),
    }, http_status=201)


# ============================================================ 管理员端：反馈管理
@bp.get("/admin/feedback")
@admin_required
def list_feedback():
    """两类反馈的合并视图。支持按 kind / status / reason_code 筛选，时间排序。"""
    kind = request.args.get("kind", "all")
    if kind not in ("all", "qa", "correction"):
        raise InvalidParam("kind 必须是 all / qa / correction 之一")
    status = request.args.get("status") or None
    if status is not None and status not in FEEDBACK_STATUSES:
        raise InvalidParam("status 必须是 pending / processing / resolved 之一")
    reason = request.args.get("reason_code") or None
    sort = request.args.get("sort", "-created_at")
    if sort not in ("-created_at", "created_at"):
        raise InvalidParam("sort 只支持 -created_at / created_at")
    page, page_size = page_args()

    rows: list[dict] = []

    if kind in ("all", "qa"):
        q = db.select(QaFeedback, User).join(User, User.id == QaFeedback.user_id)
        if status is not None:
            q = q.where(QaFeedback.status == status)
        if reason is not None:
            q = q.where(QaFeedback.reason_code == reason)
        for fb, user in db.session.execute(
            q.order_by(QaFeedback.created_at.desc()).limit(MAX_ROWS)
        ).all():
            rows.append(_serialize_qa(fb, user))

    if kind in ("all", "correction"):
        q = db.select(CorrectionFeedback, User).join(
            User, User.id == CorrectionFeedback.user_id)
        if status is not None:
            q = q.where(CorrectionFeedback.status == status)
        if reason is not None:
            q = q.where(CorrectionFeedback.reason_code == reason)
        for fb, user in db.session.execute(
            q.order_by(CorrectionFeedback.created_at.desc()).limit(MAX_ROWS)
        ).all():
            rows.append(_serialize_correction(fb, user))

    # 合并排序：默认新在前。两类各自已按时间排好，这里统一按 created_at 再排一次
    rows.sort(key=lambda r: r["created_at"] or "", reverse=(sort == "-created_at"))

    total = len(rows)
    start = (page - 1) * page_size
    return ok(paginated(rows[start:start + page_size], total, page, page_size))


@bp.get("/admin/feedback/stats")
@admin_required
def feedback_stats():
    """反馈统计：总数 / 有帮助比 / 无帮助比 / 政策错误数 / 高频原因 / 已解决。"""
    qa_rows = db.session.execute(db.select(QaFeedback)).scalars().all()
    cor_rows = db.session.execute(db.select(CorrectionFeedback)).scalars().all()

    qa_total = len(qa_rows)
    helpful = sum(1 for r in qa_rows if r.helpful)
    unhelpful = qa_total - helpful

    reason_counter: dict[str, int] = {}
    for r in qa_rows:
        if r.reason_code:
            reason_counter[r.reason_code] = reason_counter.get(r.reason_code, 0) + 1
    for r in cor_rows:
        if r.reason_code:
            reason_counter[r.reason_code] = reason_counter.get(r.reason_code, 0) + 1

    policy_error = (
        sum(1 for r in qa_rows if r.reason_code in _POLICY_ERROR_REASONS)
        + len(cor_rows)
    )

    def by_status(rows) -> dict[str, int]:
        out = {"pending": 0, "processing": 0, "resolved": 0}
        for r in rows:
            st = r.status or "pending"
            out[st] = out.get(st, 0) + 1
        return out

    qa_status = by_status(qa_rows)
    cor_status = by_status(cor_rows)
    resolved = qa_status["resolved"] + cor_status["resolved"]

    return ok({
        "total": qa_total + len(cor_rows),
        "qa": {"total": qa_total, "helpful": helpful, "unhelpful": unhelpful,
               "helpful_rate": round(helpful / qa_total, 3) if qa_total else 0,
               "unhelpful_rate": round(unhelpful / qa_total, 3) if qa_total else 0},
        "correction_count": len(cor_rows),
        "policy_error_count": policy_error,
        "reason_distribution": [
            {"reason_code": code, "count": count}
            for code, count in sorted(reason_counter.items(), key=lambda kv: -kv[1])
        ],
        "status": {
            "pending": qa_status["pending"] + cor_status["pending"],
            "processing": qa_status["processing"] + cor_status["processing"],
            "resolved": resolved,
        },
        "generated_at": _iso(utcnow()),
    })


@bp.patch("/admin/feedback/<kind>/<feedback_id>")
@admin_required
def update_feedback(kind: str, feedback_id: str):
    """更新处理状态 + 处理说明。kind 定位到具体表（qa / correction）。"""
    if kind not in ("qa", "correction"):
        raise InvalidParam("kind 必须是 qa / correction 之一")
    body: FeedbackAdminUpdateRequest = validate(
        FeedbackAdminUpdateRequest, request.get_json(silent=True) or {})

    if kind == "qa":
        row = db.session.get(QaFeedback, feedback_id)
    else:
        row = db.session.get(CorrectionFeedback, feedback_id)
    if row is None:
        raise FeedbackNotFound(f"反馈 {feedback_id} 不存在")

    if body.status is not None:
        row.status = body.status
        # 状态离开「已解决」时清掉解决时间；进入「已解决」时记时间与处理人
        if body.status == "resolved":
            row.resolved_at = utcnow()
            if kind == "correction":
                row.resolved_by = current_user().id
        else:
            row.resolved_at = None
    if body.admin_note is not None:
        row.admin_note = body.admin_note

    _audit("feedback.resolve", f"{kind}_feedback", row.id,
           json.dumps({"status": body.status}))
    db.session.commit()

    return ok({
        "kind": kind,
        "id": row.id,
        "status": row.status,
        "admin_note": row.admin_note,
        "resolved_at": _iso(row.resolved_at),
    })
