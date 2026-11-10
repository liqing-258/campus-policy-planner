"""计划草稿与确认（M5 下半之一）。

三个刻意的设计选择，都对应一条验收用例：

1. **草稿不落任务表**（T-18）。草稿阶段只建一条 `action_plans(status=draft)`，
   任务列表随响应返回、随编辑变化、不写库。用户没点"确认"之前，
   系统里不该出现一堆他没同意过的待办——那是最容易被投诉的产品行为。

2. **没有来源日期就留空**（T-16）。政策原文写"评审后发放"，就不能推出"9 月 30 日"。
   `due_at=null` + `needs_human_confirmation=true` + 一句明确的提示，
   比一个看起来很像真的假日期安全得多。

3. **确认幂等**（T-19）。重复确认返回同一个 plan_id 与同一批任务，不重复插入。
   理由：网络重试、用户连点，都会造成重复提交；此时报错是最差的选择，
   用户会以为"上一次没成功"。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from flask import Blueprint, request

from app.auth import current_user, login_required
from app.contract import body_json, new_ulid, ok, page_args, paginated
from app.documents import _get_active_or_404
from app.errors import (
    DependencyCycle,
    Forbidden,
    InvalidParam,
    PlanNotFound,
    RulesNotReady,
    SchemaInvalid,
    StateTransitionInvalid,
)
from app.models import (
    ActionPlan,
    DocumentChunk,
    MatchResultItem,
    MatchSession,
    PolicyRule,
    Task,
    db,
)
from app.schemas import PlanConfirmRequest, PlanDraftRequest, PlanPatchRequest, validate
from shared.enums import (
    PLAN_CONFIRMED,
    PLAN_DRAFT,
    PLAN_STATUSES,
    PRIORITY_P0,
    PRIORITY_P1,
    PRIORITY_P2,
    REVIEW_APPROVED,
    RULE_DEADLINE,
    RULE_MATERIAL,
    RULE_PROCESS,
    TASK_TODO,
    UNKNOWN_MISSING_INFO,
    is_plan_transition_allowed,
)

bp = Blueprint("plans", __name__)

_DATE = re.compile(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})")
# 「当年 9 月 10 日」这类**没有年份**的写法在政策原文里极常见。
# 单独识别出来不是为了猜年份（猜错比留空更糟），而是为了把提示语说对：
# 原文给了月日，就不能告诉用户"原文未给出具体日期"。
_MONTH_DAY = re.compile(r"(?:当年|本年|今年)?\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")


def _parse_iso(value: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise InvalidParam(f"due_at 不是合法的 ISO 8601 时间：{value}")
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() + "Z" if value else None


def _date_from_text(text: str | None) -> datetime | None:
    """从规则文本里找日期。找不到就返回 None —— **绝不猜**。"""
    if not text:
        return None
    match = _DATE.search(text)
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    try:
        return datetime(year, month, day)
    except ValueError:
        return None


def _month_day_from_text(text: str | None) -> tuple[int, int] | None:
    """只取「M 月 D 日」，不拼年份。用于把提示语说准，不用于生成日期。"""
    if not text:
        return None
    match = _MONTH_DAY.search(text)
    if not match:
        return None
    month, day = (int(part) for part in match.groups())
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return month, day


def _task_from_rule(rule: PolicyRule, index: int) -> dict | None:
    """把一条规则翻译成一条待办。条件类不生成任务（它们是"资格"，不是"动作"）。"""
    if rule.rule_type == RULE_MATERIAL:
        return {
            "temp_id": f"T{index}",
            "title": f"准备材料：{rule.description}"[:100],
            "description": rule.required_information or rule.description,
            "priority": PRIORITY_P0,
            "due_at": None,
            "due_source": None,
            "needs_human_confirmation": True,
            "source_rule_code": rule.rule_code,
            "required_materials": rule.required_information,
            "dependency_ids": [],
            "note": "材料清单来自政策原文，请按学院具体要求核对份数与签章。",
        }

    if rule.rule_type == RULE_DEADLINE:
        due = _date_from_text(rule.expected_value) or _date_from_text(rule.original_quote)
        month_day = None if due else (
            _month_day_from_text(rule.expected_value) or _month_day_from_text(rule.original_quote)
        )
        if due:
            note = None
        elif month_day:
            # 有月日没年份：不拼日期（9 月 10 日可能已经过去，也可能指下一学年），
            # 但提示语必须说清"原文给了月日、缺的是年份"，否则用户会以为系统没读到。
            note = (f"政策原文为「{month_day[0]} 月 {month_day[1]} 日」，但未给出年份；"
                    f"请确认具体年份后补填截止日期。")
        else:
            note = "政策原文未给出具体日期，请自行确认截止时间。"
        return {
            "temp_id": f"T{index}",
            "title": f"注意截止时间：{rule.description}"[:100],
            "description": rule.description,
            "priority": PRIORITY_P0 if due else PRIORITY_P1,
            "due_at": _iso(due),
            "due_source": "rule" if due else None,
            # 没有来源日期时不能自己编一个：留空并要求人工补充
            "needs_human_confirmation": due is None,
            "source_rule_code": rule.rule_code,
            "required_materials": None,
            "dependency_ids": [],
            "note": note,
        }

    if rule.rule_type == RULE_PROCESS:
        return {
            "temp_id": f"T{index}",
            "title": f"办理流程：{rule.description}"[:100],
            "description": rule.description,
            "priority": PRIORITY_P2,
            "due_at": None,
            "due_source": None,
            "needs_human_confirmation": True,
            "source_rule_code": rule.rule_code,
            "required_materials": None,
            "dependency_ids": [],
            "note": "流程步骤以原文为准，如有疑问请联系学院学生工作办公室。",
        }

    return None


def create_draft(document_id: str, payload: dict, user) -> dict:
    data = validate(PlanDraftRequest, payload)
    document = _get_active_or_404(document_id)

    rules = db.session.execute(
        db.select(PolicyRule)
        .where(PolicyRule.document_id == document.id,
               PolicyRule.review_status == REVIEW_APPROVED)
        .order_by(PolicyRule.rule_type, PolicyRule.rule_code)
    ).scalars().all()
    if not rules:
        raise RulesNotReady(
            "该文件还没有已复核的规则，无法生成计划；请管理员先抽取并复核规则",
            payload={"document_id": document.id, "approved_rules": 0},
        )

    match_session_id = None
    if data.match_session_id:
        session = db.session.get(MatchSession, data.match_session_id)
        if session is None or session.user_id != user.id:
            raise InvalidParam("match_session_id 不存在或不属于当前用户")
        match_session_id = session.id

    tasks = []
    for rule in rules:
        task = _task_from_rule(rule, len(tasks) + 1)
        if task:
            tasks.append(task)

    # 匹配结果里"缺信息"的条件，转成"去补充"的待办 —— 这才是闭环：
    # 用户看到"缺 poverty_certified"，下一步应该有具体动作，而不是一句提示
    if match_session_id:
        missing = db.session.execute(
            db.select(MatchResultItem, PolicyRule)
            .join(PolicyRule, PolicyRule.id == MatchResultItem.rule_id)
            .where(MatchResultItem.session_id == match_session_id,
                   MatchResultItem.status == "unknown",
                   MatchResultItem.unknown_reason == UNKNOWN_MISSING_INFO)
        ).all()
        for item, rule in missing:
            tasks.append({
                "temp_id": f"T{len(tasks) + 1}",
                "title": f"补充信息：{rule.description}"[:100],
                "description": f"匹配结果显示缺少 {rule.field_key}，补齐后可重新匹配。",
                "priority": PRIORITY_P1,
                "due_at": None,
                "due_source": None,
                "needs_human_confirmation": True,
                "source_rule_code": rule.rule_code,
                "required_materials": None,
                "dependency_ids": [],
                "note": f"需要补充的字段：{rule.field_key}",
            })

    if not tasks:
        raise RulesNotReady(
            "该文件的规则里没有可执行的材料/截止时间/流程条款，无法生成计划任务",
            payload={"document_id": document.id, "approved_rules": len(rules)},
        )

    title = f"{document.title} · 申请计划"
    plan = ActionPlan(
        id=new_ulid(), user_id=user.id, document_id=document.id,
        match_session_id=match_session_id, title=title[:200], status=PLAN_DRAFT,
    )
    db.session.add(plan)
    db.session.commit()

    return {
        "plan_id": plan.id,
        "document_id": document.id,
        "title": plan.title,
        "status": plan.status,
        "match_session_id": match_session_id,
        "tasks": tasks,
        "notes": [t["note"] for t in tasks if t["note"]],
        "persisted": False,
        "note": "草稿尚未入库：任务只在本次响应里，确认后才会写入任务中心。",
    }


# ---------------------------------------------------------------- 校验
def _detect_cycle(tasks) -> list[str]:
    """Kahn 拓扑排序，返回剩下的（成环的）temp_id。"""
    indegree = {t.temp_id: 0 for t in tasks}
    edges: dict[str, list[str]] = {t.temp_id: [] for t in tasks}
    for task in tasks:
        for dep in task.dependency_ids:
            edges[dep].append(task.temp_id)
            indegree[task.temp_id] += 1

    queue = [tid for tid, degree in indegree.items() if degree == 0]
    visited = 0
    while queue:
        current = queue.pop()
        visited += 1
        for nxt in edges[current]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    if visited == len(tasks):
        return []
    return [tid for tid, degree in indegree.items() if degree > 0]


def confirm_plan(plan_id: str, payload: dict, user) -> dict:
    plan = db.session.get(ActionPlan, plan_id)
    if plan is None:
        raise PlanNotFound("计划不存在")
    if plan.user_id != user.id:
        raise Forbidden("无权确认他人的计划")

    if plan.status == PLAN_CONFIRMED:
        tasks = db.session.execute(
            db.select(Task).where(Task.plan_id == plan.id).order_by(Task.sort_order)
        ).scalars().all()
        return {
            "plan_id": plan.id,
            "status": plan.status,
            "already_confirmed": True,
            "task_count": len(tasks),
            "tasks": [serialize_task(t) for t in tasks],
        }
    if plan.status != PLAN_DRAFT:
        raise StateTransitionInvalid(
            f"当前状态 {plan.status} 不允许确认（允许：{PLAN_DRAFT}）"
        )

    data = validate(PlanConfirmRequest, payload)
    if not data.tasks:
        raise SchemaInvalid("任务列表不能为空：确认计划至少要有一条任务")

    temp_ids = [t.temp_id for t in data.tasks]
    if len(set(temp_ids)) != len(temp_ids):
        raise SchemaInvalid("temp_id 不能重复")

    known = set(temp_ids)
    for task in data.tasks:
        unknown = [d for d in task.dependency_ids if d not in known]
        if unknown:
            raise SchemaInvalid(
                f"任务 {task.temp_id} 依赖了不存在的任务：{'/'.join(unknown)}"
            )

    cycle = _detect_cycle(data.tasks)
    if cycle:
        raise DependencyCycle(f"任务依赖成环：{' → '.join(cycle + [cycle[0]])}")

    if data.title:
        plan.title = data.title

    # 草稿里的"来自哪条规则"「为什么没日期」必须一起落库，否则确认之后
    # 任务就断了溯源链：只剩一句标题，查不回政策原文的那一条。
    rule_codes = {t.source_rule_code for t in data.tasks if t.source_rule_code}
    rule_ids: dict[str, str] = {}
    if rule_codes:
        rows = db.session.execute(
            db.select(PolicyRule.rule_code, PolicyRule.id)
            .where(PolicyRule.document_id == plan.document_id,
                   PolicyRule.rule_code.in_(rule_codes))
        ).all()
        rule_ids = {code: rid for code, rid in rows}

    created = {}
    for order, task_data in enumerate(data.tasks):
        due_at = _parse_iso(task_data.due_at) if task_data.due_at else None
        row = Task(
            id=new_ulid(), plan_id=plan.id, user_id=user.id,
            title=task_data.title, description=task_data.description,
            required_materials=task_data.required_materials,
            due_at=due_at,
            # 有日期才谈来源：规则给的记 rule，用户自己填的记 manual。
            # 之前一律写 manual，导致"日期来自政策原文"这件事无从证明。
            due_source=(
                (task_data.due_source or "manual") if due_at else None
            ),
            priority=task_data.priority, status=TASK_TODO,
            source_document_id=plan.document_id,
            source_rule_id=rule_ids.get(task_data.source_rule_code or ""),
            needs_human_confirmation=1 if task_data.needs_human_confirmation else 0,
            note=task_data.note,
            dependency_ids=json.dumps(task_data.dependency_ids, ensure_ascii=False),
            sort_order=order,
        )
        db.session.add(row)
        created[task_data.temp_id] = row

    plan.status = PLAN_CONFIRMED
    plan.confirmed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.session.commit()

    return {
        "plan_id": plan.id,
        "status": plan.status,
        "already_confirmed": False,
        "task_count": len(created),
        "tasks": [serialize_task(created[t.temp_id]) for t in data.tasks],
    }


def serialize_task(task: Task) -> dict:
    try:
        deps = json.loads(task.dependency_ids) if task.dependency_ids else []
    except ValueError:
        deps = []
    return {
        "id": task.id,
        "plan_id": task.plan_id,
        "title": task.title,
        "description": task.description,
        "required_materials": task.required_materials,
        "priority": task.priority,
        "status": task.status,
        "due_at": _iso(task.due_at),
        "due_source": task.due_source,
        "needs_human_confirmation": bool(task.needs_human_confirmation),
        "note": task.note,
        "source_rule_id": task.source_rule_id,
        "dependency_ids": deps,
        "sort_order": task.sort_order,
        "completed_at": _iso(task.completed_at),
        "created_at": _iso(task.created_at),
    }


def serialize_plan(plan: ActionPlan) -> dict:
    return {
        "id": plan.id,
        "title": plan.title,
        "status": plan.status,
        "document_id": plan.document_id,
        "match_session_id": plan.match_session_id,
        "created_at": _iso(plan.created_at),
        "confirmed_at": _iso(plan.confirmed_at),
    }


# ---------------------------------------------------------------- 路由
@bp.post("/plans/draft")
@login_required
def draft_route():
    data = body_json()
    return ok(create_draft(data.get("document_id", ""), data, current_user()))


@bp.post("/plans/<plan_id>/confirm")
@login_required
def confirm_route(plan_id: str):
    return ok(confirm_plan(plan_id, body_json(), current_user()))


@bp.get("/plans")
@login_required
def list_plans():
    user = current_user()
    page, page_size = page_args()
    stmt = db.select(ActionPlan).where(ActionPlan.user_id == user.id)
    status = request.args.get("status")
    if status:
        if status not in PLAN_STATUSES:
            raise InvalidParam("status 取值非法")
        stmt = stmt.where(ActionPlan.status == status)
    total = db.session.execute(
        db.select(db.func.count()).select_from(stmt.subquery())
    ).scalar() or 0
    rows = db.session.execute(
        stmt.order_by(ActionPlan.created_at.desc())
        .offset((page - 1) * page_size).limit(page_size)
    ).scalars().all()
    return ok(paginated([serialize_plan(p) for p in rows], total, page, page_size))


@bp.patch("/plans/<plan_id>")
@login_required
def patch_plan(plan_id: str):
    user = current_user()
    plan = db.session.get(ActionPlan, plan_id)
    if plan is None:
        raise PlanNotFound("计划不存在")
    if plan.user_id != user.id:
        raise Forbidden("无权修改他人的计划")

    data = validate(PlanPatchRequest, body_json())
    if data.title:
        plan.title = data.title
    if data.status and data.status != plan.status:
        # 确认不走这条边：/confirm 才会校验依赖与幂等
        if not is_plan_transition_allowed(plan.status, data.status):
            raise StateTransitionInvalid(
                f"当前状态 {plan.status} 不允许转移到 {data.status}"
                f"（允许：{'/'.join(_allowed_plan_targets(plan.status)) or '无'}）"
            )
        plan.status = data.status
    db.session.commit()
    return ok(serialize_plan(plan))


def _allowed_plan_targets(status: str) -> tuple[str, ...]:
    from shared.enums import PLAN_TRANSITIONS
    return PLAN_TRANSITIONS.get(status, ())
