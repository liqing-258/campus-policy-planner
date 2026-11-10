"""任务中心（M5 下半之二）。

状态机是这一层的全部意义。`done → doing` 这类转移被拒绝，不是因为实现麻烦，
而是因为**它会让"完成时间"这个字段失去意义**：一个任务可以在完成与未完成之间反复，
`completed_at` 就不再是任何事实的记录。要重做就回到 `todo`，重做完成时写新的时间。

`same → same` 不报错而是直接返回现状：前端重试、双击都会触发，
把它当错误只会制造无意义的红色提示。
"""

from __future__ import annotations

from datetime import datetime, timezone

from flask import Blueprint, request

from app.auth import current_user, login_required
from app.contract import body_json, new_ulid, ok, page_args, paginated
from app.errors import (
    Forbidden,
    InvalidParam,
    PlanNotFound,
    StateTransitionInvalid,
)
from app.models import ActionPlan, Task, db
from app.plans import serialize_task, _parse_iso
from app.schemas import TaskCreateRequest, TaskPatchRequest, TaskStatusRequest, validate
from shared.enums import (
    PLAN_CONFIRMED,
    PRIORITIES,
    TASK_DONE,
    TASK_STATUSES,
    TASK_TODO,
    TASK_TRANSITIONS,
    is_task_transition_allowed,
)

bp = Blueprint("tasks", __name__)


def _own_task_or_raise(task_id: str, user) -> Task:
    task = db.session.get(Task, task_id)
    if task is None:
        raise PlanNotFound("任务不存在")
    if task.user_id != user.id:
        # 任务标题里可能含个人信息（"补交家庭经济困难认定材料"），
        # 因此别人的任务既不给改，也不能靠 404/403 的差异探测它是否存在
        raise Forbidden("无权操作他人的任务")
    return task


def _own_plan_or_raise(plan_id: str, user) -> ActionPlan:
    plan = db.session.get(ActionPlan, plan_id)
    if plan is None:
        raise PlanNotFound("计划不存在")
    if plan.user_id != user.id:
        raise Forbidden("无权向他人的计划添加任务")
    return plan


@bp.get("/tasks")
@login_required
def list_tasks():
    user = current_user()
    page, page_size = page_args()

    stmt = db.select(Task).where(Task.user_id == user.id)
    status = request.args.get("status")
    if status:
        if status not in TASK_STATUSES:
            raise InvalidParam("status 取值非法")
        stmt = stmt.where(Task.status == status)
    priority = request.args.get("priority")
    if priority:
        if priority not in PRIORITIES:
            raise InvalidParam("priority 取值非法")
        stmt = stmt.where(Task.priority == priority)
    plan_id = request.args.get("plan_id")
    if plan_id:
        stmt = stmt.where(Task.plan_id == plan_id)

    total = db.session.execute(
        db.select(db.func.count()).select_from(stmt.subquery())
    ).scalar() or 0
    rows = db.session.execute(
        stmt.order_by(Task.sort_order, Task.created_at)
        .offset((page - 1) * page_size).limit(page_size)
    ).scalars().all()
    return ok(paginated([serialize_task(t) for t in rows], total, page, page_size))


@bp.post("/tasks")
@login_required
def create_task():
    user = current_user()
    data = validate(TaskCreateRequest, body_json())
    plan = _own_plan_or_raise(data.plan_id, user)

    # 未确认的计划里没有任务表记录，手工加一条会造成"有任务但没有确认过计划"的脏状态
    if plan.status != PLAN_CONFIRMED:
        raise StateTransitionInvalid(
            f"计划当前状态为 {plan.status}，请先确认计划再添加任务"
        )

    due_at = _parse_iso(data.due_at) if data.due_at else None
    sort_order = db.session.execute(
        db.select(db.func.coalesce(db.func.max(Task.sort_order), -1))
        .where(Task.plan_id == plan.id)
    ).scalar() + 1

    task = Task(
        id=new_ulid(), plan_id=plan.id, user_id=user.id,
        title=data.title, description=data.description,
        required_materials=data.required_materials,
        due_at=due_at, due_source="manual" if due_at else None,
        priority=data.priority, status=TASK_TODO,
        source_document_id=plan.document_id, sort_order=sort_order,
    )
    db.session.add(task)
    db.session.commit()
    # 不要把状态码拼成 tuple 再套一层：`ok(x), 201` 得到的是 ((Response, 200), 201)，
    # Flask 直接抛 "did not return a valid response"，用户看到的是 500。
    return ok(serialize_task(task), http_status=201)


@bp.patch("/tasks/<task_id>")
@login_required
def patch_task(task_id: str):
    user = current_user()
    task = _own_task_or_raise(task_id, user)
    data = validate(TaskPatchRequest, body_json())
    changes = data.model_dump(exclude_unset=True)

    for key, value in changes.items():
        if key == "due_at":
            task.due_at = _parse_iso(value) if value else None
            task.due_source = "manual" if value else None
        else:
            setattr(task, key, value)
    db.session.commit()
    return ok(serialize_task(task))


@bp.post("/tasks/<task_id>/status")
@login_required
def update_status(task_id: str):
    user = current_user()
    task = _own_task_or_raise(task_id, user)
    data = validate(TaskStatusRequest, body_json())

    if data.status == task.status:
        # 幂等：前端重试不该看到红色报错
        return ok(serialize_task(task))

    if not is_task_transition_allowed(task.status, data.status):
        allowed = TASK_TRANSITIONS.get(task.status, ())
        raise StateTransitionInvalid(
            f"当前状态 {task.status} 不允许转移到 {data.status}"
            f"（允许：{'/'.join(allowed) or '无，这是终态'}）"
        )

    task.status = data.status
    if data.status == TASK_DONE:
        task.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    elif task.completed_at is not None:
        # 撤销完成时清空时间，否则"未完成但有完成时间"就是个自相矛盾的记录
        task.completed_at = None
    db.session.commit()
    return ok(serialize_task(task))
