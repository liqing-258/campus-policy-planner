"""我的校园日历（R14）。

## 这一层不新建"第二套任务"

需求要求"将现有任务中心中的任务按照日期显示在日历上"，所以日历**读的是
`tasks` 表本身**，不是它的一份副本：改日期走既有的 `PATCH /tasks/{id}`，
标完成走既有的 `POST /tasks/{id}/status`。任务中心与日历看到的是同一行数据，
"同步"因此不是一件需要实现的事——它是没有副本的自然结果。

## 只有个人日程是新表

`tasks.plan_id` 是 `NOT NULL` 外键，而"周三去图书馆还书"不属于任何一份政策计划。
绕开它有两条路，都不通：给 tasks 加列会让用 `create_all()` 建起来的老库报
`no such column`；伪造一条计划与一份文件则会被 `PRAGMA foreign_keys=ON`
拦下，或者在知识库里多出一份不存在的"政策"。所以个人日程单独一张
`calendar_events`，在**接口层**与任务合并成同一种形状，前端只认一种。

## 不猜年份：这一层最容易被做错的地方

政策原文写「当年 9 月 10 日 17:00」，没有年份。日期的处理只有三种合法结果：

1. `due_source=rule`：原文给了**完整年月日** → 进日历格子，标「政策原文」；
2. 原文只给月日、学生补了年份 → 进格子，标「你补全年份」，`raw_text` 保留
   「9 月 10 日」原样，可回溯；
3. 什么都没给 → **不进格子**，进「待确认日期」清单，按缺法分成
   「年份待确认 / 原文未给日期 / 未设置日期」三种。

`_undated_reason_of` 里没有"猜一个年份"的分支，`_calendar_date_of` 也没有
"用今年补上"的分支。这不是遗漏：9 月 10 日可能已经过去，也可能指下一学年，
两种猜测都会生成一个看起来很像真的假截止日期——比空着危险得多。

## 分类不落库

六类日程里，任务的类别按 `rule_type + 所属文档标题` 实时推断。落库的话，
规则重新抽取、文件换版之后分类就过期了，而它又没有任何"过期"标记。
代价是推断认不出的会进 `other`（诚实地说"其他事务"），不会硬塞进六类之一。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from flask import Blueprint, request

from app.auth import current_user, login_required
from app.contract import body_json, new_ulid, ok
from app.errors import Forbidden, InvalidParam, PlanNotFound
from app.models import (
    ActionPlan,
    CalendarEvent,
    Document,
    PolicyRule,
    Task,
    db,
)
from app.plans import _date_from_text, _iso, _month_day_from_text
from app.schemas import (
    CalendarEventCreateRequest,
    CalendarEventPatchRequest,
    validate,
)
from shared.enums import (
    CALENDAR_CATEGORIES,
    CALENDAR_CATEGORY_LABELS,
    CAL_AWARD,
    CAL_EXAM,
    CAL_LIBRARY,
    CAL_MATERIAL,
    CAL_OTHER,
    CAL_PERSONAL,
    CAL_SCHOLARSHIP,
    DATE_SOURCE_CONFIRMED,
    DATE_SOURCE_LABELS,
    DATE_SOURCE_MANUAL,
    DATE_SOURCE_POLICY,
    EFFECTIVE_EXPIRED,
    EFFECTIVE_SUPERSEDED,
    REVIEW_APPROVED,
    RULE_DEADLINE,
    RULE_MATERIAL,
    TASK_CANCELLED,
    TASK_DONE,
    UNDATED_NO_DATE,
    UNDATED_NOT_SET,
    UNDATED_REASON_LABELS,
    UNDATED_YEAR_MISSING,
)

bp = Blueprint("calendar", __name__)

MAX_RANGE_DAYS = 366
MAX_UNDATED = 60

# 文档标题里的主题词。**顺序有意义**：「计算机等级考试」同时含"考试"与
# "等级考试"，先判考试就不会落进"其他"；「推荐免试攻读研究生」含"推免"，
# 归评奖评优而不是奖学金。
#
# `award` 里**刻意不放「奖学金评审」**：「本科生奖学金评审办法」里的 9 月 10 日
# 是奖学金申请截止，不是评优时间。第一版放了这个词，日历上所有奖学金截止
# 都被染成"评奖评优"色，需求里并列的两类直接并成了一类。
_TOPIC_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (CAL_EXAM, ("四六级", "英语四", "六级", "四级", "等级考试", "资格考试",
                "考试报名", "报名须知", "计算机等级", "职业资格")),
    (CAL_LIBRARY, ("图书馆", "借阅", "还书", "图书")),
    (CAL_AWARD, ("评奖评优", "优秀学生", "三好", "先进班级", "先进班集体",
                 "推免", "遴选", "综合素质测评")),
    (CAL_SCHOLARSHIP, ("奖学金", "助学金", "助学贷款", "勤工助学", "困难认定", "资助")),
)


# ---------------------------------------------------------------- 小工具
def _today() -> date:
    """"今天"按**服务器本地时区**算。

    与 `tasks/stats` 的 `utc_now` 不同，这里必须用本地日期：用户在 11 月 16 日
    凌晨 1 点打开日历，看到的"今天"应该是 16 号，而不是 UTC 的 15 号。
    逾期判断仍用 UTC 时刻（`_now()`），因为那是与 `due_at` 同一把尺子。
    """
    return datetime.now().date()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_day(raw: str | None, field: str) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        raise InvalidParam(f"{field} 不是合法日期（应为 YYYY-MM-DD）：{raw}")


def _month_span(anchor: date) -> tuple[date, date]:
    first = anchor.replace(day=1)
    if first.month == 12:
        last = date(first.year + 1, 1, 1) - timedelta(days=1)
    else:
        last = date(first.year, first.month + 1, 1) - timedelta(days=1)
    return first, last


def _range_of(args) -> tuple[date, date]:
    anchor = _today()
    start = _parse_day(args.get("from"), "from")
    end = _parse_day(args.get("to"), "to")

    if start is None and end is None:
        return _month_span(anchor)
    if start is None:
        start = end.replace(day=1)
    if end is None:
        end = _month_span(start)[1]
    if start > end:
        raise InvalidParam("from 不能晚于 to")
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise InvalidParam(f"单次查询跨度不能超过 {MAX_RANGE_DAYS} 天")
    return start, end


def _day_bounds(start: date, end: date) -> tuple[datetime, datetime]:
    return (
        datetime.combine(start, time.min),
        datetime.combine(end + timedelta(days=1), time.min),
    )


def _category_of(rule_type: str | None, doc_title: str | None,
                 has_rule: bool) -> str:
    """任务 → 六类之一。

    `has_rule=False` 表示这条任务不是任何规则生成的（学生在计划里自己补的待办），
    它本质上就是个人事务。这一条比任何标题关键词都可靠。
    """
    if not has_rule:
        return CAL_PERSONAL
    if rule_type == RULE_MATERIAL:
        return CAL_MATERIAL
    if rule_type != RULE_DEADLINE:
        # 办理流程、资格条件、补充信息都不属于"某类时间节点"，
        # 硬按文档标题分成"奖学金截止"会让人以为那是申请截止日
        return CAL_OTHER
    title = doc_title or ""
    for category, words in _TOPIC_WORDS:
        if any(word in title for word in words):
            return category
    return CAL_OTHER


def _date_source_of(task: Task) -> str | None:
    """这个日期从哪来。没有日期就没有来源。"""
    if task.due_at is None:
        return None
    if task.due_source == "rule":
        return DATE_SOURCE_POLICY
    # 日期被改过（PATCH 一律置 manual），但来源规则还在：
    # 只有原文给过月日、学生补年份这一种，才配叫"你补全年份"。
    # 原文根本没给日期的，学生填的那天是他自己定的，必须说"手动填写"。
    if task.source_rule_id and _undated_reason_of(task)[0] == UNDATED_YEAR_MISSING:
        return DATE_SOURCE_CONFIRMED
    return DATE_SOURCE_MANUAL


def _undated_reason_of(task: Task) -> tuple[str, str | None]:
    """没有日期时，缺的到底是什么。返回 (原因码, 原文里的月日)。

    只读 `note` 与 `description`，不读标题——标题里出现过"9 月 10 日"这种
    片段的情况不存在，而 `note` 是确认计划时**逐字**写进去的。
    """
    text = " ".join(part for part in (task.note, task.description) if part)
    month_day = _month_day_from_text(task.note) or _month_day_from_text(task.description)
    raw = f"{month_day[0]} 月 {month_day[1]} 日" if month_day else None

    if "未给出年份" in text:
        return UNDATED_YEAR_MISSING, raw
    if "未给出具体日期" in text or "未给出日期" in text:
        return UNDATED_NO_DATE, raw
    return UNDATED_NOT_SET, raw


def _time_of(value: datetime | None) -> str | None:
    """只在"不是 00:00"时返回时间。

    政策日期一律落在 00:00，把它显示成「00:00 截止」会让用户以为系统精确到分钟，
    而原文只写了日期。
    """
    if value is None or (value.hour == 0 and value.minute == 0):
        return None
    return value.strftime("%H:%M")


def _priority_rank(priority: str | None) -> int:
    return {"P0": 0, "P1": 1, "P2": 2, "P3": 3}.get(priority or "P2", 2)


def _category_payload(category: str) -> dict:
    return {
        "category": category,
        "category_label": CALENDAR_CATEGORY_LABELS.get(category, category),
    }


# ---------------------------------------------------------------- 条目构造
def _task_item(task: Task, *, doc_titles: dict[str, str],
               rule_types: dict[str, str], plan_titles: dict[str, str],
               now: datetime, today: date) -> dict:
    source = _date_source_of(task)
    finished = task.status == TASK_DONE
    cancelled = task.status == TASK_CANCELLED
    day = task.due_at.date() if task.due_at else None
    overdue = bool(
        task.due_at is not None and not finished and not cancelled and task.due_at < now
    )
    doc_title = doc_titles.get(task.source_document_id or "")
    category = _category_of(
        rule_types.get(task.source_rule_id or ""),
        doc_title,
        task.source_rule_id is not None,
    )

    return {
        "id": f"task:{task.id}",
        "kind": "task",
        "ref_id": task.id,
        "title": task.title,
        "date": day.isoformat() if day else None,
        "time": _time_of(task.due_at),
        "due_at": _iso(task.due_at),
        "status": task.status,
        "done": finished,
        "cancelled": cancelled,
        "overdue": overdue,
        "priority": task.priority,
        "date_source": source,
        "date_source_label": DATE_SOURCE_LABELS.get(source or "", "无日期"),
        "date_confirmed_by_user": bool(
            day is not None and not finished and not cancelled and day < today
            and source != DATE_SOURCE_POLICY
        ),
        **_category_payload(category),
        "plan_id": task.plan_id,
        "plan_title": plan_titles.get(task.plan_id),
        "document_id": task.source_document_id,
        "document_title": doc_title,
        "description": task.description,
        "required_materials": task.required_materials,
        "note": task.note,
        "needs_human_confirmation": bool(task.needs_human_confirmation),
        "completed_at": _iso(task.completed_at),
        "raw_text": None,
        "undated_reason": None,
        "undated_reason_label": None,
        "editable": False,
        "deletable": False,
    }


def _event_item(event: CalendarEvent, today: date) -> dict:
    overdue = event.event_date < today
    return {
        "id": f"event:{event.id}",
        "kind": "event",
        "ref_id": event.id,
        "title": event.title,
        "date": event.event_date.isoformat(),
        "time": event.event_time,
        "due_at": None,
        "status": "todo",
        "done": False,
        "cancelled": False,
        "overdue": overdue,
        "priority": None,
        "date_source": DATE_SOURCE_MANUAL,
        "date_source_label": DATE_SOURCE_LABELS[DATE_SOURCE_MANUAL],
        "date_confirmed_by_user": False,
        **_category_payload(event.category),
        "plan_id": None,
        "plan_title": None,
        "document_id": None,
        "document_title": None,
        "description": None,
        "required_materials": None,
        "note": event.note,
        "needs_human_confirmation": False,
        "completed_at": None,
        "raw_text": None,
        "undated_reason": None,
        "undated_reason_label": None,
        "editable": True,
        "deletable": True,
    }


def _rule_item(rule: PolicyRule, doc: Document, *, now: datetime,
               today: date) -> dict:
    """已复核、但还没有生成任务的截止时间规则。

    为什么要把它们放进日历：学生可能只做了匹配、没确认计划。这时候任务表里
    一条都没有，日历全空——但政策里明写着"9 月 10 日截止"。这不是"没有日程"，
    是"有时间节点、还没变成我的待办"，两件事必须分开说。
    """
    due = _date_from_text(rule.expected_value) or _date_from_text(rule.original_quote)
    overdue = bool(due is not None and due < now)
    category = _category_of(RULE_DEADLINE, doc.title, True)
    return {
        "id": f"rule:{rule.id}",
        "kind": "policy",
        "ref_id": rule.id,
        "title": f"截止时间：{rule.description}"[:120],
        "date": due.date().isoformat() if due else None,
        "time": _time_of(due),
        "due_at": _iso(due),
        "status": "policy",
        "done": False,
        "cancelled": False,
        "overdue": overdue,
        "priority": None,
        "date_source": DATE_SOURCE_POLICY,
        "date_source_label": DATE_SOURCE_LABELS[DATE_SOURCE_POLICY],
        "date_confirmed_by_user": False,
        **_category_payload(category),
        "plan_id": None,
        "plan_title": None,
        "document_id": doc.id,
        "document_title": doc.title,
        "description": rule.description,
        "required_materials": None,
        "note": "该时间节点来自政策原文，尚未生成待办任务；可在事项详情里生成办理计划。",
        "needs_human_confirmation": False,
        "completed_at": None,
        "raw_text": None,
        "undated_reason": None,
        "undated_reason_label": None,
        "editable": False,
        "deletable": False,
    }


def _undated_item_from_task(task: Task, *, doc_titles: dict[str, str],
                            rule_types: dict[str, str],
                            plan_titles: dict[str, str]) -> dict:
    reason, raw = _undated_reason_of(task)
    doc_title = doc_titles.get(task.source_document_id or "")
    category = _category_of(
        rule_types.get(task.source_rule_id or ""), doc_title,
        task.source_rule_id is not None,
    )
    return {
        "id": f"task:{task.id}",
        "kind": "task",
        "ref_id": task.id,
        "title": task.title,
        "date": None,
        "time": None,
        "due_at": None,
        "status": task.status,
        "done": task.status == TASK_DONE,
        "cancelled": task.status == TASK_CANCELLED,
        "overdue": False,
        "priority": task.priority,
        "date_source": None,
        "date_source_label": "无日期",
        **_category_payload(category),
        "plan_id": task.plan_id,
        "plan_title": plan_titles.get(task.plan_id),
        "document_id": task.source_document_id,
        "document_title": doc_title,
        "description": task.description,
        "required_materials": task.required_materials,
        "note": task.note,
        "needs_human_confirmation": bool(task.needs_human_confirmation),
        "completed_at": _iso(task.completed_at),
        "raw_text": raw,
        "undated_reason": reason,
        "undated_reason_label": UNDATED_REASON_LABELS.get(reason, "未设置日期"),
        "editable": True,
        "deletable": False,
    }


def _undated_item_from_rule(rule: PolicyRule, doc: Document) -> dict:
    month_day = _month_day_from_text(rule.expected_value) or \
        _month_day_from_text(rule.original_quote)
    if month_day:
        reason, raw = UNDATED_YEAR_MISSING, f"{month_day[0]} 月 {month_day[1]} 日"
    else:
        reason, raw = UNDATED_NO_DATE, None
    return {
        "id": f"rule:{rule.id}",
        "kind": "policy",
        "ref_id": rule.id,
        "title": f"截止时间：{rule.description}"[:120],
        "date": None,
        "time": None,
        "due_at": None,
        "status": "policy",
        "done": False,
        "cancelled": False,
        "overdue": False,
        "priority": None,
        "date_source": None,
        "date_source_label": "无日期",
        **_category_payload(_category_of(RULE_DEADLINE, doc.title, True)),
        "plan_id": None,
        "plan_title": None,
        "document_id": doc.id,
        "document_title": doc.title,
        "description": rule.description,
        "required_materials": None,
        "note": "政策原文只给出月日、未给出年份，系统不会替你补年份；"
                "请以学院通知的年份为准。",
        "needs_human_confirmation": True,
        "completed_at": None,
        "raw_text": raw,
        "undated_reason": reason,
        "undated_reason_label": UNDATED_REASON_LABELS.get(reason, "未设置日期"),
        "editable": False,
        "deletable": False,
    }


# ---------------------------------------------------------------- 装配
def _load_maps(user, tasks: list[Task]) -> tuple[dict, dict, dict]:
    doc_ids = {t.source_document_id for t in tasks if t.source_document_id}
    rule_ids = {t.source_rule_id for t in tasks if t.source_rule_id}
    plan_ids = {t.plan_id for t in tasks if t.plan_id}

    doc_titles: dict[str, str] = {}
    if doc_ids:
        rows = db.session.execute(
            db.select(Document.id, Document.title).where(Document.id.in_(doc_ids))
        ).all()
        doc_titles = {doc_id: title for doc_id, title in rows}

    rule_types: dict[str, str] = {}
    if rule_ids:
        rows = db.session.execute(
            db.select(PolicyRule.id, PolicyRule.rule_type)
            .where(PolicyRule.id.in_(rule_ids))
        ).all()
        rule_types = {rid: rtype for rid, rtype in rows}

    plan_titles: dict[str, str] = {}
    if plan_ids:
        rows = db.session.execute(
            db.select(ActionPlan.id, ActionPlan.title)
            .where(ActionPlan.id.in_(plan_ids), ActionPlan.user_id == user.id)
        ).all()
        plan_titles = {pid: title for pid, title in rows}

    return doc_titles, rule_types, plan_titles


def _policy_rules_without_tasks(user) -> list[tuple[PolicyRule, Document]]:
    """已复核的截止时间规则里，**还没变成这个用户的任务**的那些。

    只取现行有效的文件：已被替代的旧版写的截止日期，今年已经不适用了。
    把它显示出来比不显示更糟——用户会按旧日期准备材料。
    """
    generated = db.session.execute(
        db.select(Task.source_rule_id)
        .where(Task.user_id == user.id, Task.source_rule_id.is_not(None))
    ).scalars().all()
    skip = set(generated)

    rows = db.session.execute(
        db.select(PolicyRule, Document)
        .join(Document, Document.id == PolicyRule.document_id)
        .where(
            PolicyRule.rule_type == RULE_DEADLINE,
            PolicyRule.review_status == REVIEW_APPROVED,
            Document.deleted_at.is_(None),
            Document.effective_status.notin_((EFFECTIVE_SUPERSEDED, EFFECTIVE_EXPIRED)),
        )
        .order_by(Document.title, PolicyRule.rule_code)
    ).all()
    return [(rule, doc) for rule, doc in rows if rule.id not in skip]


def build_calendar(user, start: date, end: date) -> dict:
    now = _now()
    today = _today()
    start_dt, end_dt = _day_bounds(start, end)

    tasks = db.session.execute(
        db.select(Task)
        .where(
            Task.user_id == user.id,
            Task.due_at.is_not(None),
            Task.due_at >= start_dt,
            Task.due_at < end_dt,
            Task.status != TASK_CANCELLED,
        )
        .order_by(Task.due_at, Task.sort_order)
    ).scalars().all()

    events = db.session.execute(
        db.select(CalendarEvent)
        .where(
            CalendarEvent.user_id == user.id,
            CalendarEvent.event_date >= start,
            CalendarEvent.event_date <= end,
        )
        .order_by(CalendarEvent.event_date, CalendarEvent.event_time)
    ).scalars().all()

    doc_titles, rule_types, plan_titles = _load_maps(user, list(tasks))

    items = [
        _task_item(t, doc_titles=doc_titles, rule_types=rule_types,
                   plan_titles=plan_titles, now=now, today=today)
        for t in tasks
    ]
    items += [_event_item(e, today) for e in events]

    # 政策里写明的截止节点：只要还没生成任务，就作为"参考日程"放进同一天，
    # 但 kind='policy'，前端把它画成空心/虚线，与"我的待办"区分开。
    for rule, doc in _policy_rules_without_tasks(user):
        due = _date_from_text(rule.expected_value) or _date_from_text(rule.original_quote)
        if due is None:
            continue
        if not (start_dt <= due < end_dt):
            continue
        items.append(_rule_item(rule, doc, now=now, today=today))

    items.sort(key=lambda it: (
        it["date"] or "9999-12-31",
        it["time"] or "99:99",
        _priority_rank(it["priority"]),
        it["title"],
    ))
    for index, item in enumerate(items, start=1):
        item["index"] = index

    by_day: dict[str, int] = {}
    by_category = {key: 0 for key in CALENDAR_CATEGORIES}
    for item in items:
        if item["date"]:
            by_day[item["date"]] = by_day.get(item["date"], 0) + 1
        by_category[item["category"]] = by_category.get(item["category"], 0) + 1

    undated = _build_undated(user, doc_titles, rule_types, plan_titles)
    # 待确认清单也按类别计数：日历上一格都没有时，图例显示全 0 会让人以为
    # "这些政策跟日历无关"，而实际上它们只是缺一个年份。
    undated_by_category = {key: 0 for key in CALENDAR_CATEGORIES}
    for item in undated:
        undated_by_category[item["category"]] = undated_by_category.get(item["category"], 0) + 1

    return {
        "range": {
            "from": start.isoformat(),
            "to": end.isoformat(),
            "days": (end - start).days + 1,
        },
        "today": today.isoformat(),
        "items": items,
        "undated": undated,
        "counts": {
            "total": len(items),
            "done": sum(1 for it in items if it["done"]),
            "overdue": sum(1 for it in items if it["overdue"]),
            "undated": len(undated),
            "by_day": by_day,
            "by_category": by_category,
            "undated_by_category": undated_by_category,
        },
        "categories": [
            {
                "key": key,
                "label": CALENDAR_CATEGORY_LABELS[key],
                "count": by_category.get(key, 0),
            }
            for key in CALENDAR_CATEGORIES
        ],
        "notice": (
            "政策原文只给出月日、没有年份的，不会自动补年份，统一列在「待确认日期」里；"
            "确认年份后才会出现在日历上。"
        ),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _build_undated(user, doc_titles, rule_types, plan_titles) -> list[dict]:
    """没有日期的未完成任务 + 只给月日的政策节点。

    只看未完成、未取消的：一条已完成的任务没有日期，不值得占用"待确认"的位置。
    """
    rows = db.session.execute(
        db.select(Task)
        .where(
            Task.user_id == user.id,
            Task.due_at.is_(None),
            Task.status.notin_((TASK_DONE, TASK_CANCELLED)),
        )
        .order_by(Task.priority, Task.sort_order)
    ).scalars().all()

    # 传进来的三张映射是"范围内有日期那批任务"的。待确认这批是另一批行，
    # 必须用自己的行重建：否则它们的文档标题与规则类型全查不到，
    # 表现是 25 条「准备材料」被整批归成「其他事务」，而日历上一格都还没有，
    # 图例全 0，看起来像"这些政策跟日历无关"。
    if rows:
        doc_titles, rule_types, plan_titles = _load_maps(user, rows)

    undated = [
        _undated_item_from_task(t, doc_titles=doc_titles,
                                rule_types=rule_types, plan_titles=plan_titles)
        for t in rows
    ]
    # 「年份待确认」排最前：它是唯一"只差一个数字就能落进日历"的一类，
    # 也是需求里专门点名的那一类。
    order = {UNDATED_YEAR_MISSING: 0, UNDATED_NO_DATE: 1, UNDATED_NOT_SET: 2}
    undated.sort(key=lambda it: (
        order.get(it["undated_reason"] or UNDATED_NOT_SET, 3),
        it["title"],
    ))

    for rule, doc in _policy_rules_without_tasks(user):
        due = _date_from_text(rule.expected_value) or _date_from_text(rule.original_quote)
        if due is not None:
            continue
        undated.insert(0, _undated_item_from_rule(rule, doc))

    return undated[:MAX_UNDATED]


def build_agenda(user, days: int) -> dict:
    """工作台「近期重要日程」：逾期 + 未来 N 天内到期 + 待确认日期。"""
    now = _now()
    today = _today()
    horizon = today + timedelta(days=days)

    tasks = db.session.execute(
        db.select(Task)
        .where(Task.user_id == user.id, Task.due_at.is_not(None))
        .order_by(Task.due_at)
    ).scalars().all()
    events = db.session.execute(
        db.select(CalendarEvent)
        .where(CalendarEvent.user_id == user.id,
               CalendarEvent.event_date <= horizon)
        .order_by(CalendarEvent.event_date)
    ).scalars().all()

    doc_titles, rule_types, plan_titles = _load_maps(user, list(tasks))

    overdue: list[dict] = []
    soon: list[dict] = []
    for task in tasks:
        item = _task_item(task, doc_titles=doc_titles, rule_types=rule_types,
                          plan_titles=plan_titles, now=now, today=today)
        if item["cancelled"] or item["done"]:
            continue
        if item["overdue"]:
            overdue.append(item)
        elif item["date"] and today.isoformat() <= item["date"] <= horizon.isoformat():
            soon.append(item)

    for event in events:
        item = _event_item(event, today)
        if item["overdue"]:
            overdue.append(item)
        elif today.isoformat() <= item["date"] <= horizon.isoformat():
            soon.append(item)

    overdue.sort(key=lambda it: (it["date"] or "", it["time"] or ""))
    soon.sort(key=lambda it: (it["date"] or "", it["time"] or "99:99"))

    undated = _build_undated(user, doc_titles, rule_types, plan_titles)
    # 工作台只放"只差一个数字"的那一类；「原文未给日期」在日历页看全量
    year_missing = [it for it in undated if it["undated_reason"] == UNDATED_YEAR_MISSING]

    return {
        "today": today.isoformat(),
        "window_days": days,
        "overdue": overdue,
        "soon": soon,
        "undated": year_missing[:5],
        "counts": {
            "overdue": len(overdue),
            "soon": len(soon),
            "undated": len(undated),
            "undated_year_missing": len(year_missing),
        },
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


# ---------------------------------------------------------------- 个人日程 CRUD
def _own_event_or_raise(event_id: str, user) -> CalendarEvent:
    event = db.session.get(CalendarEvent, event_id)
    if event is None:
        raise PlanNotFound("日程不存在")
    if event.user_id != user.id:
        # 日程标题可能含私人安排，别人的既不返回也不允许改
        raise Forbidden("无权操作他人的日程")
    return event


def serialize_event(event: CalendarEvent) -> dict:
    return {
        "id": event.id,
        "title": event.title,
        "date": event.event_date.isoformat(),
        "time": event.event_time,
        "note": event.note,
        "category": event.category,
        "category_label": CALENDAR_CATEGORY_LABELS.get(event.category, event.category),
        "created_at": _iso(event.created_at),
        "updated_at": _iso(event.updated_at),
    }


@bp.get("/calendar")
@login_required
def get_calendar():
    start, end = _range_of(request.args)
    return ok(build_calendar(current_user(), start, end))


@bp.get("/calendar/agenda")
@login_required
def get_agenda():
    raw = request.args.get("days", "7")
    try:
        days = int(raw)
    except ValueError:
        raise InvalidParam("days 必须为整数")
    if days < 1 or days > 60:
        raise InvalidParam("days 必须在 1–60 之间")
    return ok(build_agenda(current_user(), days))


@bp.post("/calendar/events")
@login_required
def create_event():
    user = current_user()
    data = validate(CalendarEventCreateRequest, body_json())
    event = CalendarEvent(
        id=new_ulid(),
        user_id=user.id,
        title=data.title,
        note=data.note,
        event_date=date.fromisoformat(data.date),
        event_time=data.time or None,
        category=data.category,
    )
    db.session.add(event)
    db.session.commit()
    return ok(serialize_event(event), http_status=201)


@bp.patch("/calendar/events/<event_id>")
@login_required
def patch_event(event_id: str):
    event = _own_event_or_raise(event_id, current_user())
    data = validate(CalendarEventPatchRequest, body_json())
    changes = data.model_dump(exclude_unset=True)

    for key, value in changes.items():
        if key == "date":
            event.event_date = date.fromisoformat(value)
        elif key == "time":
            # 显式传空串 = 取消具体时间；不传 = 不动
            event.event_time = value or None
        else:
            setattr(event, key, value)
    db.session.commit()
    return ok(serialize_event(event))


@bp.delete("/calendar/events/<event_id>")
@login_required
def delete_event(event_id: str):
    event = _own_event_or_raise(event_id, current_user())
    db.session.delete(event)
    db.session.commit()
    return ok({"id": event_id, "deleted": True})
