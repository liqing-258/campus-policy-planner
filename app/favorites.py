"""常用政策收藏（R17）。

## 一张关联表，四类目标

收藏只存「谁 + 收藏了什么」：`favorites` 表用 `user_id + target_type + target_id`
指向四类既有实体，**不改动**政策文件 / 指南 / 问答 / 计划任何一张表的结构。
四类目标的落点：

| target_type | 含义 | target_id 指向 | 关联政策文件 |
| --- | --- | --- | --- |
| document | 政策文件 | documents.id | 它自己 |
| guide | 校园事务指南事项 | guides 编目 key | 编目实时解析出的政策 |
| qa | 重要政策问答 | messages.id（助手回答） | 第一条引用指向的文件 |
| plan | 常用办理事项 | action_plans.id（事务计划） | plan.document_id |

## 快照与实时两条线

- **快照**（落库）：`title`（收藏那一刻的名称，目标删除后仍能显示）、
  `document_id`（关联政策的**指向**）。
- **实时**（出参现算）：`effective_status` 等状态一律 JOIN 目标对象现查，
  于是「政策更新或失效后同步显示最新有效状态」成立——快照存的是指向，不是状态。
  收藏的问答也实时回 `messages.content` 与 `citations_json`，天然保留原始回答与来源，
  不会因为政策后续更新而丢失上下文。

## 幂等

同一个人对同一目标只留一条（唯一约束）。重复收藏是覆盖快照、返回 `updated`，
不是新增——与 qa_feedback 的幂等方式一致。取消收藏对不存在的记录也返回成功。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from flask import Blueprint, request
from sqlalchemy.exc import IntegrityError

from app.auth import current_user, login_required
from app.contract import new_ulid, ok, page_args, paginated
from app.errors import (
    DocumentNotFound,
    Forbidden,
    GuideNotFound,
    InvalidParam,
    MessageNotFound,
    PlanNotFound,
)
from app.models import (
    ActionPlan,
    AuditLog,
    Conversation,
    Document,
    Favorite,
    Message,
    db,
    utcnow,
)
from app.schemas import FavoriteCreateRequest, validate
from shared.enums import (
    FAVORITE_DOCUMENT,
    FAVORITE_GUIDE,
    FAVORITE_PLAN,
    FAVORITE_QA,
    FAVORITE_TARGETS,
)

bp = Blueprint("favorites", __name__)

_FAVORITE_LABELS = {
    FAVORITE_DOCUMENT: "政策文件",
    FAVORITE_GUIDE: "校园事务指南",
    FAVORITE_QA: "重要问答",
    FAVORITE_PLAN: "常用办理事项",
}

# 收藏列表单次最多返回多少条：个人收藏量级小，这个上限只防库被塞爆后接口转圈
MAX_ROWS = 500


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _audit(action: str, resource_id: str | None, detail: str | None = None) -> None:
    """审计只追加，失败不阻断主流程（与 documents._audit 同源）。"""
    try:
        db.session.add(AuditLog(
            id=new_ulid(),
            user_id=current_user().id,
            action=action,
            resource_type="favorite",
            resource_id=resource_id,
            detail_json=detail,
            request_id=request.headers.get("X-Request-Id"),
        ))
    except Exception:
        pass


def _doc_brief(doc: Document | None) -> dict | None:
    if doc is None:
        return None
    return {
        "id": doc.id,
        "title": doc.title,
        "effective_status": doc.effective_status,
        "academic_year": doc.academic_year,
        "version_label": doc.version_label,
        "department": doc.department,
    }


def _citations_of(message: Message | None) -> list[dict]:
    if message is None or not message.citations_json:
        return []
    try:
        data = json.loads(message.citations_json)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def _question_of(message: Message | None) -> str | None:
    """同会话里、该回答之前最近的一条用户提问（与 feedback.py 同源）。"""
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


# ---------------------------------------------------------------- 解析目标
def _resolve_title_doc(target_type: str, target_id: str, user_id: str) -> tuple[str, str | None]:
    """收藏时解析「名称快照」与「关联政策文件 id」。

    返回 (title, document_id)。目标不存在 / 不属于当前用户时抛对应业务异常。
    """
    if target_type == FAVORITE_DOCUMENT:
        doc = db.session.get(Document, target_id)
        if doc is None or doc.deleted_at is not None:
            raise DocumentNotFound("文档不存在或已被删除")
        return doc.title, doc.id

    if target_type == FAVORITE_GUIDE:
        from app.guides import _ITEM_BY_KEY, _pick_document

        spec = _ITEM_BY_KEY.get(target_id)
        if spec is None:
            raise GuideNotFound()
        docs = db.session.execute(
            db.select(Document).where(Document.deleted_at.is_(None))
        ).scalars().all()
        doc = _pick_document(docs, spec["doc_key"])
        return spec["title"], (doc.id if doc is not None else None)

    if target_type == FAVORITE_QA:
        message = db.session.get(Message, target_id)
        if message is None:
            raise MessageNotFound(f"消息 {target_id} 不存在")
        conversation = db.session.get(Conversation, message.conversation_id)
        if conversation is None or conversation.user_id != user_id:
            # 与反馈接口同口径：不区分「会话没了」与「不是你的」，避免泄露 message_id
            raise Forbidden("只能收藏自己会话里的回答")
        if message.role != "assistant":
            raise InvalidParam("只能收藏助手的回答")
        question = _question_of(message)
        title = (question or message.content)[:200]
        citations = _citations_of(message)
        document_id = None
        if citations and isinstance(citations[0], dict):
            document_id = citations[0].get("document_id")
        return title, document_id

    if target_type == FAVORITE_PLAN:
        plan = db.session.get(ActionPlan, target_id)
        if plan is None:
            raise PlanNotFound(f"计划 {target_id} 不存在")
        if plan.user_id != user_id:
            raise Forbidden("只能收藏自己的事务计划")
        return plan.title, plan.document_id

    raise InvalidParam(f"未知的收藏类型：{target_type}")


# ============================================================ 学生端：收藏
@bp.post("/favorites")
@login_required
def create_favorite():
    """收藏一个目标。幂等：同 user + target 已存在则覆盖快照并返回 updated=true。"""
    body: FavoriteCreateRequest = validate(
        FavoriteCreateRequest, request.get_json(silent=True) or {})
    user = current_user()

    title, document_id = _resolve_title_doc(body.target_type, body.target_id, user.id)

    existing = db.session.execute(
        db.select(Favorite).where(
            Favorite.user_id == user.id,
            Favorite.target_type == body.target_type,
            Favorite.target_id == body.target_id,
        )
    ).scalar_one_or_none()

    updated = existing is not None
    if existing is None:
        row = Favorite(
            id=new_ulid(), user_id=user.id,
            target_type=body.target_type, target_id=body.target_id,
            title=title, document_id=document_id,
        )
        db.session.add(row)
    else:
        row = existing
        row.title = title
        row.document_id = document_id

    _audit("favorite.create", row.id,
           json.dumps({"target_type": body.target_type, "target_id": body.target_id}))
    # 并发下的 check-then-act 竞态：两个请求都查到"不存在"，第二个提交时撞唯一约束。
    # 唯一约束挡住重复数据是对的（R19 并发实测：5 线程仍只留 1 条），但用户不该看到 500——
    # 这个接口的语义本来就是幂等，所以回滚后重查一次，按"已收藏"返回 updated=true。
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        row = db.session.execute(
            db.select(Favorite).where(
                Favorite.user_id == user.id,
                Favorite.target_type == body.target_type,
                Favorite.target_id == body.target_id,
            )
        ).scalar_one_or_none()
        if row is None:
            # 不是唯一约束造成的（比如外键失败）：照原样抛，别把真错误吞成 200
            raise
        updated = True

    return ok({
        "id": row.id,
        "target_type": row.target_type,
        "target_type_label": _FAVORITE_LABELS.get(row.target_type, row.target_type),
        "target_id": row.target_id,
        "title": row.title,
        "document_id": row.document_id,
        "updated": updated,
        "created_at": _iso(row.created_at),
    }, http_status=201)


@bp.delete("/favorites/<target_type>/<target_id>")
@login_required
def remove_favorite(target_type: str, target_id: str):
    """取消收藏。幂等：不存在的记录也返回成功（取消一个没收藏的东西不算错误）。"""
    if target_type not in FAVORITE_TARGETS:
        raise InvalidParam("未知的收藏类型")
    user = current_user()
    row = db.session.execute(
        db.select(Favorite).where(
            Favorite.user_id == user.id,
            Favorite.target_type == target_type,
            Favorite.target_id == target_id,
        )
    ).scalar_one_or_none()

    if row is not None:
        db.session.delete(row)
        _audit("favorite.remove", row.id,
               json.dumps({"target_type": target_type, "target_id": target_id}))
        db.session.commit()

    return ok({"target_type": target_type, "target_id": target_id, "removed": row is not None})


@bp.get("/favorites/ids")
@login_required
def favorite_ids():
    """当前用户已收藏的目标标识集合（`target_type:target_id`）。

    给收藏按钮渲染状态用：前端挂载时拉一次，就能知道每处「已收藏 / 未收藏」。
    """
    user = current_user()
    rows = db.session.execute(
        db.select(Favorite).where(Favorite.user_id == user.id)
    ).scalars().all()
    return ok({"ids": [f"{r.target_type}:{r.target_id}" for r in rows], "count": len(rows)})


# ============================================================ 学生端：收藏列表
@bp.get("/favorites")
@login_required
def list_favorites():
    """我的收藏列表。支持按类型筛选 + 关键词搜索 + 分页。"""
    user = current_user()
    target_type = request.args.get("type") or ""
    if target_type and target_type not in FAVORITE_TARGETS:
        raise InvalidParam("type 必须是 document / guide / qa / plan 之一")
    keyword = (request.args.get("q") or "").strip()
    page, page_size = page_args()

    query = db.select(Favorite).where(Favorite.user_id == user.id)
    if target_type:
        query = query.where(Favorite.target_type == target_type)
    # 关键词不在这里用 SQL 过滤：qa 的回答正文、question 都不在 title 快照里，
    # SQL 只按 title 过滤会把「回答命中关键词」的收藏提前排掉。收藏量级小，
    # 拉全量后统一在内存里按 title / question / answer 搜。

    rows = db.session.execute(
        query.order_by(Favorite.created_at.desc()).limit(MAX_ROWS)
    ).scalars().all()

    # guide 的「关联政策 + 有效状态」要实时解析（自动跟随新版），一次性加载 docs 复用
    docs = db.session.execute(
        db.select(Document).where(Document.deleted_at.is_(None))
    ).scalars().all()

    items = [_serialize(r, docs) for r in rows]

    if keyword:
        lowered = keyword.lower()
        def body_hit(item: dict) -> bool:
            if lowered in (item.get("title") or "").lower():
                return True
            if item.get("question") and lowered in item["question"].lower():
                return True
            if item.get("answer") and lowered in item["answer"].lower():
                return True
            return False
        items = [it for it in items if body_hit(it)]

    total = len(items)
    start = (page - 1) * page_size
    window = items[start:start + page_size]

    return ok({
        **paginated(window, total, page, page_size),
        "type": target_type or None,
        "query": keyword,
        "types": [
            {"id": t, "label": _FAVORITE_LABELS[t]} for t in FAVORITE_TARGETS
        ],
    })


def _serialize(fav: Favorite, docs: list[Document]) -> dict:
    """把一条收藏序列化成统一形状。目标状态一律实时 JOIN，不读快照状态。"""
    doc = None
    effective_status = None
    question = None
    answer = None
    citations = None
    answered_at = None
    plan_status = None
    conversation_id = None

    if fav.target_type == FAVORITE_DOCUMENT:
        doc = db.session.get(Document, fav.target_id)
        if doc is not None and doc.deleted_at is None:
            effective_status = doc.effective_status
        else:
            doc = None

    elif fav.target_type == FAVORITE_GUIDE:
        from app.guides import _ITEM_BY_KEY, _pick_document

        spec = _ITEM_BY_KEY.get(fav.target_id)
        if spec is not None:
            picked = _pick_document(docs, spec["doc_key"])
            if picked is not None:
                doc = picked
                effective_status = picked.effective_status

    elif fav.target_type == FAVORITE_QA:
        message = db.session.get(Message, fav.target_id)
        if message is not None:
            question = _question_of(message)
            answer = message.content
            citations = _citations_of(message)
            answered_at = _iso(message.created_at)
            conversation_id = message.conversation_id
            if citations and isinstance(citations[0], dict):
                cited_id = citations[0].get("document_id")
                if cited_id:
                    cited_doc = db.session.get(Document, cited_id)
                    if cited_doc is not None and cited_doc.deleted_at is None:
                        doc = cited_doc
                        effective_status = cited_doc.effective_status

    elif fav.target_type == FAVORITE_PLAN:
        plan = db.session.get(ActionPlan, fav.target_id)
        if plan is not None:
            plan_status = plan.status
            if plan.document_id:
                plan_doc = db.session.get(Document, plan.document_id)
                if plan_doc is not None and plan_doc.deleted_at is None:
                    doc = plan_doc
                    effective_status = plan_doc.effective_status

    return {
        "id": fav.id,
        "target_type": fav.target_type,
        "target_type_label": _FAVORITE_LABELS.get(fav.target_type, fav.target_type),
        "target_id": fav.target_id,
        "title": fav.title,
        "created_at": _iso(fav.created_at),
        "document": _doc_brief(doc),
        "effective_status": effective_status,
        "question": question,
        "answer": answer,
        "citations": citations,
        "answered_at": answered_at,
        "plan_status": plan_status,
        "conversation_id": conversation_id,
    }
