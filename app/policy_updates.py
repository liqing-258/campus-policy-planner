"""政策更新提醒（R15）：近期入库 / 近期元数据变化的政策，对学生的工作台推送。

一条不变的边界（需求四）：
    **只提示版本发生变化，不编造条款差异。**
系统不做条款级 diff（两版正文的语义对齐做不准，做不准的 diff 比没有 diff 更害人），
所以卡片上只有事实字段——标题、发布日期、有效状态、上传/更新时间、与我的计划的关联——
一句"发生了什么变化"都不许出现，指向原文让用户自己看。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from flask import Blueprint, request

from app import versions
from app.auth import current_user, login_required
from app.contract import ok
from app.errors import InvalidParam
from app.models import ActionPlan, Document, db

bp = Blueprint("policies", __name__)

# 窗口上限 90 天：更新提醒是"最近发生了什么"，不是档案检索；
# 要更早的用知识库列表（它有完整的筛选）。
MAX_WINDOW_DAYS = 90
DEFAULT_WINDOW_DAYS = 30
MAX_ITEMS = 20


@bp.get("/policies/updates")
@login_required
def policy_updates():
    """近期政策更新（学生工作台「政策更新」区）。

    两类都算"更新"，但分开标注，不混为一谈：

    - `new_file`：窗口内**新入库**（新政策或新版本上传）；
    - `meta_updated`：窗口内**元数据变化**（常见是有效状态被调整，例如旧版被标
      「已被替代」）。注意重新解析（reparse）也会动 `updated_at`，所以这一类的
      文案只说"文件信息有更新"，不猜是哪种变化。
    """
    raw = request.args.get("days", str(DEFAULT_WINDOW_DAYS))
    try:
        days = int(raw)
    except ValueError:
        raise InvalidParam("days 必须是 1–90 的整数")
    if not 1 <= days <= MAX_WINDOW_DAYS:
        raise InvalidParam("days 必须是 1–90 的整数")

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    window = now - timedelta(days=days)

    rows = db.session.execute(
        db.select(Document)
        .where(
            Document.deleted_at.is_(None),
            (Document.created_at >= window) | (Document.updated_at >= window),
        )
        .order_by(Document.updated_at.desc())
        .limit(MAX_ITEMS)
    ).scalars().all()

    # 我的计划：按 document_id 关联；再按版本家族扩一圈——
    # 旧版被替代后，与旧版关联的计划同样受新版本影响。
    plans = db.session.execute(
        db.select(ActionPlan).where(ActionPlan.user_id == current_user().id)
    ).scalars().all()
    plans_by_doc: dict[str, list[ActionPlan]] = {}
    for plan in plans:
        if plan.document_id:
            plans_by_doc.setdefault(plan.document_id, []).append(plan)

    items = []
    for doc in rows:
        family = versions.related_versions(doc)
        family_ids = {d.id for d in family}
        related_plans = []
        for fid in family_ids:
            for plan in plans_by_doc.get(fid, []):
                related_plans.append({"id": plan.id, "title": plan.title,
                                      "document_id": plan.document_id})
        newer = [d for d in family if d.id != doc.id and
                 (d.published_at or d.created_at.date()) >
                 (doc.published_at or doc.created_at.date())]
        items.append({
            "document_id": doc.id,
            "title": doc.title,
            "department": doc.department,
            "published_at": doc.published_at.isoformat() if doc.published_at else None,
            "academic_year": doc.academic_year,
            "version_label": doc.version_label,
            "effective_status": doc.effective_status or "unknown",
            "created_at": doc.created_at.isoformat() + "Z",
            "updated_at": doc.updated_at.isoformat() + "Z",
            "change_kind": "new_file" if doc.created_at >= window else "meta_updated",
            # 事实性提示，不是条款 diff
            "change_summary": ("新文件入库（可能是新版本）" if doc.created_at >= window
                               else "文件信息有更新（如有效状态调整）"),
            "version_count": len(family),
            "has_newer": bool(newer),
            "newer_document_id": newer[0].id if newer else None,
            "related_plans": related_plans,
            "may_affect_plans": bool(related_plans),
        })

    return ok({
        "window_days": days,
        "today": now.date().isoformat(),
        "items": items,
        "counts": {
            "total": len(items),
            "new_file": sum(1 for i in items if i["change_kind"] == "new_file"),
            "meta_updated": sum(1 for i in items if i["change_kind"] == "meta_updated"),
            "plan_related": sum(1 for i in items if i["may_affect_plans"]),
        },
        # 固定声明：写死在这里，前端原样展示，防止任何一层"顺手"编出变化内容
        "notice": "系统只提示版本与状态变化，不比对条款内容；具体差异请打开原文核对。",
    })
