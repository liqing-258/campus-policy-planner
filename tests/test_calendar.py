"""我的校园日历（R14）测试。

这一层最容易出的错不是 500，而是**日期错得很像真的**：

- 政策只写「当年 9 月 10 日」，系统自己补一个年份，画进 2026 年 9 月的格子；
- 换了任务日期，日历还显示旧的那天（因为日历另存了一份）；
- 任务标完成之后，日历上还是待办；
- 跨月查询把上个月最后一天算进来，或者把下个月第一天漏掉；
- 别人建的日程能被当前用户改。

所以断言打在**具体日期字符串、具体缺日期原因、具体 kind** 上，
而不是"接口 200 且数组非空"。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.contract import new_ulid
from app.models import (
    ActionPlan,
    CalendarEvent,
    Document,
    DocumentChunk,
    PolicyRule,
    Task,
    User,
    db,
)

SCHOLARSHIP = "本科生奖学金评审办法"
LIBRARY = "图书馆借阅与自习空间管理办法"
EXAM = "全国大学英语四六级考试报名须知"

QUOTE_YEAR_MISSING = "每学年秋季学期集中受理一次，申请材料提交截止时间为当年 9 月 10 日 17:00。"
QUOTE_NO_DATE = "评审结束后由学校统一发放，具体时间另行通知。"


# ---------------------------------------------------------------- 夹具
def _login(client, username="student", password="student123") -> dict:
    resp = client.post("/api/v1/auth/login",
                       json={"username": username, "password": password})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return {"Authorization": f"Bearer {resp.get_json()['data']['token']}"}


def _student(app) -> User:
    return db.session.execute(
        db.select(User).where(User.username == "student")
    ).scalars().first()


def _doc(app, title: str, *, effective: str = "active") -> str:
    with app.app_context():
        doc = Document(
            id=new_ulid(), title=title, file_name=f"{title}.pdf",
            stored_name=f"{new_ulid()}.pdf", file_type="pdf", file_size=1024,
            file_hash=new_ulid(), effective_status=effective,
            parse_status="indexed", uploader_id=_student(app).id,
        )
        db.session.add(doc)
        db.session.commit()
        return doc.id


def _plan(app, document_id: str, *, status: str = "confirmed") -> str:
    with app.app_context():
        plan = ActionPlan(
            id=new_ulid(), user_id=_student(app).id, document_id=document_id,
            title="测试计划", status=status,
        )
        db.session.add(plan)
        db.session.commit()
        return plan.id


def _task(app, plan_id: str, title: str, *, due_at: datetime | None = None,
          status: str = "todo", due_source: str | None = None,
          rule_id: str | None = None, note: str | None = None,
          document_id: str | None = None) -> str:
    with app.app_context():
        plan = db.session.get(ActionPlan, plan_id)
        task = Task(
            id=new_ulid(), plan_id=plan_id, user_id=_student(app).id,
            title=title, due_at=due_at,
            due_source=due_source if due_at else None,
            priority="P0", status=status,
            source_document_id=document_id or plan.document_id,
            source_rule_id=rule_id, note=note,
            needs_human_confirmation=0, sort_order=0,
            dependency_ids="[]",
        )
        db.session.add(task)
        db.session.commit()
        return task.id


def _rule(app, document_id: str, *, rule_type: str, quote: str,
          expected: str | None = None, review: str = "approved",
          description: str = "测试规则") -> str:
    with app.app_context():
        chunk = DocumentChunk(
            id=new_ulid(), document_id=document_id,
            chunk_index=db.session.execute(
                db.select(db.func.count()).select_from(DocumentChunk)
                .where(DocumentChunk.document_id == document_id)
            ).scalar() or 0,
            content=quote, char_start=0, char_end=len(quote), token_count=10,
        )
        db.session.add(chunk)
        db.session.commit()
        rule = PolicyRule(
            id=new_ulid(), document_id=document_id, chunk_id=chunk.id,
            rule_code=f"R{new_ulid()[:6]}", rule_type=rule_type,
            description=description, expected_value=expected or quote,
            original_quote=quote, quote_verified=True,
            extraction_status="verified", review_status=review,
        )
        db.session.add(rule)
        db.session.commit()
        return rule.id


@pytest.fixture()
def base(app):
    """一份奖学金文件 + 一个已确认计划。规则、任务由各用例自己加。"""
    doc_id = _doc(app, SCHOLARSHIP)
    plan_id = _plan(app, doc_id)
    return {"doc_id": doc_id, "plan_id": plan_id}


def _get(client, headers, query: str = "") -> dict:
    resp = client.get(f"/api/v1/calendar{query}", headers=headers)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["code"] == 0, body
    return body["data"]


def _by_id(data: dict, item_id: str) -> dict:
    for item in data["items"]:
        if item["id"] == item_id:
            return item
    raise AssertionError(f"{item_id} 不在 {[it['id'] for it in data['items']]}")


# ---------------------------------------------------------------- 日程显示
def test_month_view_returns_task_with_date(app, client, base):
    task_id = _task(app, base["plan_id"], "提交申请表",
                    due_at=datetime(2026, 11, 20, 9, 0), due_source="rule")
    data = _get(client, _login(client), "?from=2026-11-01&to=2026-11-30")
    item = _by_id(data, f"task:{task_id}")
    assert item["date"] == "2026-11-20"
    assert item["time"] == "09:00"
    assert item["kind"] == "task"
    assert item["date_source"] == "policy"
    assert item["date_source_label"] == "政策原文"
    assert data["range"]["days"] == 30
    assert data["counts"]["total"] == 1


def test_midnight_due_shows_no_time(app, client, base):
    """政策日期落在 00:00 时不能显示「00:00 截止」——原文只写了日期。"""
    task_id = _task(app, base["plan_id"], "提交材料",
                    due_at=datetime(2026, 11, 20, 0, 0), due_source="rule")
    data = _get(client, _login(client), "?from=2026-11-01&to=2026-11-30")
    assert _by_id(data, f"task:{task_id}")["time"] is None


def test_cancelled_task_is_hidden(app, client, base):
    _task(app, base["plan_id"], "作废的待办", due_at=datetime(2026, 11, 20, 9, 0),
          status="cancelled")
    data = _get(client, _login(client), "?from=2026-11-01&to=2026-11-30")
    assert data["items"] == []


def test_completed_task_still_shows_with_done_flag(app, client, base):
    """需求 3.6：已完成的任务要能在日历上看到，并带完成状态。"""
    task_id = _task(app, base["plan_id"], "已交材料",
                    due_at=datetime(2026, 11, 10, 9, 0), status="done",
                    due_source="rule")
    data = _get(client, _login(client), "?from=2026-11-01&to=2026-11-30")
    item = _by_id(data, f"task:{task_id}")
    assert item["done"] is True
    assert item["status"] == "done"
    assert data["counts"]["done"] == 1
    assert item["overdue"] is False


# ---------------------------------------------------------------- 跨月 / 范围
def test_range_excludes_last_day_of_previous_month(app, client, base):
    _task(app, base["plan_id"], "十月最后一天", due_at=datetime(2026, 10, 31, 23, 0))
    _task(app, base["plan_id"], "十一月第一天", due_at=datetime(2026, 11, 1, 0, 30))
    data = _get(client, _login(client), "?from=2026-11-01&to=2026-11-30")
    assert [it["title"] for it in data["items"]] == ["十一月第一天"]


def test_range_includes_last_day_of_month(app, client, base):
    """月末 23:59 必须属于本月——`< 下月 1 日 00:00` 的右开区间最容易漏。"""
    _task(app, base["plan_id"], "月末最后一刻", due_at=datetime(2026, 11, 30, 23, 59))
    data = _get(client, _login(client), "?from=2026-11-01&to=2026-11-30")
    assert [it["title"] for it in data["items"]] == ["月末最后一刻"]


def test_cross_year_range(app, client, base):
    _task(app, base["plan_id"], "元旦前的截止", due_at=datetime(2026, 12, 31, 10, 0))
    _task(app, base["plan_id"], "开学第一周", due_at=datetime(2027, 1, 5, 10, 0))
    data = _get(client, _login(client), "?from=2026-12-01&to=2027-01-31")
    assert [it["date"] for it in data["items"]] == ["2026-12-31", "2027-01-05"]
    assert data["range"]["days"] == 62


def test_default_range_is_current_month(app, client):
    data = _get(client, _login(client))
    today = date.today()
    assert data["range"]["from"] == today.replace(day=1).isoformat()
    assert data["range"]["to"].startswith(today.strftime("%Y-%m"))


def test_invalid_ranges_rejected(app, client):
    headers = _login(client)
    bad = client.get("/api/v1/calendar?from=2026-12-01&to=2026-11-01", headers=headers)
    assert bad.get_json()["code"] == 1001
    wide = client.get("/api/v1/calendar?from=2024-01-01&to=2026-11-01", headers=headers)
    assert wide.get_json()["code"] == 1001
    malformed = client.get("/api/v1/calendar?from=2026-13-01", headers=headers)
    assert malformed.get_json()["code"] == 1001


# ---------------------------------------------------------------- 不猜年份
def test_year_missing_is_not_guessed(app, client, base):
    """需求五的原例：只写「9 月 10 日」，必须有「年份待确认」，不能生成日期。"""
    rule_id = _rule(app, base["doc_id"], rule_type="deadline",
                    quote=QUOTE_YEAR_MISSING, expected="当年 9 月 10 日 17:00")
    task_id = _task(
        app, base["plan_id"], "注意截止时间：申请材料提交截止时间为当年 9 月 10 日 17:00",
        due_at=None, rule_id=rule_id,
        note="政策原文为「9 月 10 日」，但未给出年份；请确认具体年份后补填截止日期。",
    )
    data = _get(client, _login(client), "?from=2026-09-01&to=2026-09-30")

    assert data["items"] == [], "缺年份的任务绝不能出现在任何一天的格子里"
    undated = [it for it in data["undated"] if it["id"] == f"task:{task_id}"]
    assert len(undated) == 1
    assert undated[0]["undated_reason"] == "year_missing"
    assert undated[0]["undated_reason_label"] == "年份待确认"
    assert undated[0]["raw_text"] == "9 月 10 日"
    assert undated[0]["date"] is None


def test_undated_category_uses_its_own_document(app, client, base):
    """待确认条目与"有日期"条目是两批行，映射必须各自构建。

    第一版共用一份映射（用空的有日期那批建的），27 条「准备材料」被整批
    归成「其他事务」，图例全 0——看起来像这些政策跟日历无关，实际只是缺日期。
    """
    rule_id = _rule(app, base["doc_id"], rule_type="material",
                    quote="申请须提交申请表（一式两份）。")
    _task(app, base["plan_id"], "准备材料：申请表", rule_id=rule_id,
          note="政策原文未给出具体日期，请自行确认截止时间。")
    data = _get(client, _login(client), "?from=2026-11-01&to=2026-11-30")
    assert data["items"] == []
    assert data["undated"][0]["category"] == "material"
    assert data["counts"]["undated_by_category"]["material"] == 1


def test_undated_category_uses_its_own_document(app, client, base):
    """待确认条目与"有日期"条目是两批行，映射必须各自构建。

    第一版共用一份映射（用空的有日期那批建的），27 条「准备材料」被整批
    归成「其他事务」，图例全 0——看起来像这些政策跟日历无关，实际只是缺日期。
    """
    rule_id = _rule(app, base["doc_id"], rule_type="material",
                    quote="申请须提交申请表（一式两份）。")
    _task(app, base["plan_id"], "准备材料：申请表", rule_id=rule_id,
          note="政策原文未给出具体日期，请自行确认截止时间。")
    data = _get(client, _login(client), "?from=2026-11-01&to=2026-11-30")
    assert data["items"] == []
    assert data["undated"][0]["category"] == "material"
    assert data["counts"]["undated_by_category"]["material"] == 1


def test_undated_reasons_are_three_different_things(app, client, base):
    rule_id = _rule(app, base["doc_id"], rule_type="deadline", quote=QUOTE_NO_DATE)
    year_missing = _task(app, base["plan_id"], "缺年份", rule_id=rule_id,
                         note="政策原文为「9 月 10 日」，但未给出年份；请确认具体年份后补填截止日期。")
    no_date = _task(app, base["plan_id"], "原文没给", rule_id=rule_id,
                    note="政策原文未给出具体日期，请自行确认截止时间。")
    not_set = _task(app, base["plan_id"], "本来就不用日期",
                    note="流程步骤以原文为准，如有疑问请联系学院学生工作办公室。")

    data = _get(client, _login(client), "?from=2026-11-01&to=2026-11-30")
    reasons = {it["id"]: it["undated_reason"] for it in data["undated"]}
    assert reasons[f"task:{year_missing}"] == "year_missing"
    assert reasons[f"task:{no_date}"] == "no_date_in_policy"
    assert reasons[f"task:{not_set}"] == "not_set"
    # 「年份待确认」排在最前：它只差一个数字就能落进日历
    assert data["undated"][0]["id"] == f"task:{year_missing}"


def test_year_missing_rule_without_task_appears_as_policy_node(app, client, base):
    """已复核的截止规则、但学生还没生成计划：要显示政策节点，且同样不猜年份。"""
    _rule(app, base["doc_id"], rule_type="deadline",
          quote=QUOTE_YEAR_MISSING, expected="当年 9 月 10 日 17:00",
          description="申请材料提交截止时间为当年 9 月 10 日 17:00")
    data = _get(client, _login(client), "?from=2026-09-01&to=2026-09-30")
    assert data["items"] == []
    policy = [it for it in data["undated"] if it["kind"] == "policy"]
    assert len(policy) == 1
    assert policy[0]["raw_text"] == "9 月 10 日"
    assert policy[0]["undated_reason"] == "year_missing"


def test_unreviewed_deadline_rule_is_not_shown(app, client, base):
    _rule(app, base["doc_id"], rule_type="deadline", quote=QUOTE_YEAR_MISSING,
          expected="当年 9 月 10 日 17:00", review="needs_review")
    data = _get(client, _login(client), "?from=2026-09-01&to=2026-09-30")
    assert data["items"] == []
    assert data["undated"] == []


def test_superseded_document_rule_is_not_shown(app, client):
    """旧版政策写的截止日期今年已经不适用，显示出来比不显示更糟。"""
    old_doc = _doc(app, "旧版奖学金办法", effective="superseded")
    _rule(app, old_doc, rule_type="deadline",
          quote="2025 年 9 月 10 日前提交申请。", expected="2025-09-10")
    data = _get(client, _login(client), "?from=2025-09-01&to=2025-09-30")
    assert data["items"] == []


def test_policy_rule_with_full_date_is_shown_once(app, client, base):
    """有完整年月日的规则：在格子里出现，且标 kind=policy（还不是我的待办）。"""
    _rule(app, base["doc_id"], rule_type="deadline",
          quote="2026 年 11 月 20 日前提交申请。", expected="2026-11-20",
          description="2026 年 11 月 20 日前提交申请")
    data = _get(client, _login(client), "?from=2026-11-01&to=2026-11-30")
    assert len(data["items"]) == 1
    assert data["items"][0]["kind"] == "policy"
    assert data["items"][0]["date"] == "2026-11-20"


def test_policy_rule_disappears_after_task_is_generated(app, client, base):
    """同一条规则不能既算"政策节点"又算"我的任务"——重复显示会让人以为有两件事。"""
    rule_id = _rule(app, base["doc_id"], rule_type="deadline",
                    quote="2026 年 11 月 20 日前提交申请。", expected="2026-11-20")
    _task(app, base["plan_id"], "截止时间：2026 年 11 月 20 日前提交申请",
          due_at=datetime(2026, 11, 20, 0, 0), due_source="rule", rule_id=rule_id)
    data = _get(client, _login(client), "?from=2026-11-01&to=2026-11-30")
    assert len(data["items"]) == 1
    assert data["items"][0]["kind"] == "task"


# ---------------------------------------------------------------- 分类
def test_categories_follow_rule_type_and_document(app, client, base):
    material_rule = _rule(app, base["doc_id"], rule_type="material",
                          quote="申请须提交申请表（一式两份）。")
    deadline_rule = _rule(app, base["doc_id"], rule_type="deadline",
                          quote="2026 年 11 月 21 日前提交。", expected="2026-11-21")

    lib_doc = _doc(app, LIBRARY)
    exam_doc = _doc(app, EXAM)
    lib_plan = _plan(app, lib_doc)
    exam_plan = _plan(app, exam_doc)
    lib_rule = _rule(app, lib_doc, rule_type="deadline",
                     quote="借阅期限 30 天，2026 年 11 月 22 日前归还。",
                     expected="2026-11-22")
    exam_rule = _rule(app, exam_doc, rule_type="deadline",
                      quote="2026 年 11 月 23 日前完成报名，逾期不予补报。",
                      expected="2026-11-23")

    material = _task(app, base["plan_id"], "准备材料：申请表",
                     due_at=datetime(2026, 11, 18, 9, 0), rule_id=material_rule)
    deadline = _task(app, base["plan_id"], "奖学金截止",
                     due_at=datetime(2026, 11, 21, 9, 0), rule_id=deadline_rule)
    library = _task(app, lib_plan, "归还借阅图书",
                    due_at=datetime(2026, 11, 22, 9, 0), rule_id=lib_rule)
    exam = _task(app, exam_plan, "四六级报名",
                 due_at=datetime(2026, 11, 23, 9, 0), rule_id=exam_rule)
    personal = _task(app, base["plan_id"], "自己加的待办",
                     due_at=datetime(2026, 11, 24, 9, 0))

    data = _get(client, _login(client), "?from=2026-11-01&to=2026-11-30")
    got = {it["id"]: it["category"] for it in data["items"]}
    assert got[f"task:{material}"] == "material"
    assert got[f"task:{deadline}"] == "scholarship"
    assert got[f"task:{library}"] == "library"
    assert got[f"task:{exam}"] == "exam"
    assert got[f"task:{personal}"] == "personal"


def test_category_exam_wins_over_scholarship(app, client, base):
    """「计算机等级考试」含"考试"，不能被"奖学金"之类的词先命中。"""
    doc = _doc(app, "计算机等级考试与职业资格证书管理办法")
    plan = _plan(app, doc)
    rule_id = _rule(app, doc, rule_type="deadline",
                    quote="2026 年 11 月 25 日前完成报名。", expected="2026-11-25")
    task_id = _task(app, plan, "报名截止",
                    due_at=datetime(2026, 11, 25, 9, 0), rule_id=rule_id)
    data = _get(client, _login(client), "?from=2026-11-01&to=2026-11-30")
    assert _by_id(data, f"task:{task_id}")["category"] == "exam"


def test_category_counts_cover_all_seven_types(app, client, base):
    data = _get(client, _login(client), "?from=2026-11-01&to=2026-11-30")
    keys = [row["key"] for row in data["categories"]]
    assert len(keys) == 7
    assert keys[0] == "scholarship" and keys[-1] == "other"


# ---------------------------------------------------------------- 任务联动
def test_task_date_change_is_visible_immediately(app, client, base):
    """需求 3.4：改个人任务日期，任务中心与日历同步。

    日历没有副本，所以"同步"= 同一次 PATCH 之后重新查，日期就变了。
    """
    task_id = _task(app, base["plan_id"], "改期任务", due_at=datetime(2026, 11, 18, 9, 0))
    headers = _login(client)

    patched = client.patch(f"/api/v1/tasks/{task_id}",
                           json={"due_at": "2026-11-25T09:00:00Z"}, headers=headers)
    assert patched.status_code == 200, patched.get_data(as_text=True)

    data = _get(client, headers, "?from=2026-11-01&to=2026-11-30")
    item = _by_id(data, f"task:{task_id}")
    assert item["date"] == "2026-11-25"
    assert item["date_source"] == "manual"

    # 任务中心读同一行：没有任何一份副本需要跟着改
    listing = client.get(f"/api/v1/tasks?plan_id={base['plan_id']}", headers=headers)
    rows = listing.get_json()["data"]["items"]
    assert [r["due_at"] for r in rows if r["id"] == task_id][0].startswith("2026-11-25")


def test_task_moved_out_of_month_disappears(app, client, base):
    task_id = _task(app, base["plan_id"], "移到下个月", due_at=datetime(2026, 11, 18, 9, 0))
    headers = _login(client)
    client.patch(f"/api/v1/tasks/{task_id}",
                 json={"due_at": "2026-12-02T09:00:00Z"}, headers=headers)
    data = _get(client, headers, "?from=2026-11-01&to=2026-11-30")
    assert data["items"] == []


def test_task_completion_syncs_to_calendar_and_agenda(app, client, base):
    task_id = _task(app, base["plan_id"], "今天到期", due_at=datetime(2026, 11, 20, 9, 0))
    headers = _login(client)
    resp = client.post(f"/api/v1/tasks/{task_id}/status", json={"status": "done"},
                       headers=headers)
    assert resp.status_code == 200, resp.get_data(as_text=True)

    data = _get(client, headers, "?from=2026-11-01&to=2026-11-30")
    assert _by_id(data, f"task:{task_id}")["done"] is True

    agenda = client.get("/api/v1/calendar/agenda?days=7", headers=headers).get_json()["data"]
    assert f"task:{task_id}" not in [it["id"] for it in agenda["soon"]]
    assert f"task:{task_id}" not in [it["id"] for it in agenda["overdue"]]


def test_completing_a_policy_dated_task_keeps_its_source(app, client, base):
    """学生补年份之后，日期来源要变成「你补全年份」，不能退化成"手动填写"。"""
    rule_id = _rule(app, base["doc_id"], rule_type="deadline", quote=QUOTE_YEAR_MISSING)
    task_id = _task(app, base["plan_id"], "缺年份的截止", rule_id=rule_id,
                    note="政策原文为「9 月 10 日」，但未给出年份；请确认具体年份后补填截止日期。")
    headers = _login(client)
    client.patch(f"/api/v1/tasks/{task_id}",
                 json={"due_at": "2026-09-10T00:00:00Z"}, headers=headers)
    data = _get(client, headers, "?from=2026-09-01&to=2026-09-30")
    item = _by_id(data, f"task:{task_id}")
    assert item["date"] == "2026-09-10"
    assert item["date_source"] == "confirmed"
    assert item["date_source_label"] == "你补全年份"


def test_task_without_rule_and_with_date_is_manual(app, client, base):
    task_id = _task(app, base["plan_id"], "自己定的日子",
                    due_at=datetime(2026, 11, 19, 9, 0))
    data = _get(client, _login(client), "?from=2026-11-01&to=2026-11-30")
    item = _by_id(data, f"task:{task_id}")
    assert item["category"] == "personal"
    assert item["date_source"] == "manual"


# ---------------------------------------------------------------- 个人日程
def test_event_crud(app, client):
    headers = _login(client)
    created = client.post("/api/v1/calendar/events", headers=headers, json={
        "title": "去图书馆还书", "date": "2026-11-21", "time": "15:30",
        "note": "两本", "category": "library",
    })
    assert created.status_code == 201, created.get_data(as_text=True)
    event = created.get_json()["data"]
    assert event["date"] == "2026-11-21" and event["time"] == "15:30"

    data = _get(client, headers, "?from=2026-11-01&to=2026-11-30")
    item = _by_id(data, f"event:{event['id']}")
    assert item["kind"] == "event"
    assert item["editable"] is True and item["deletable"] is True
    assert item["category_label"] == "图书归还"

    moved = client.patch(f"/api/v1/calendar/events/{event['id']}", headers=headers,
                         json={"date": "2026-11-28", "title": "去图书馆还书（改期）"})
    assert moved.get_json()["data"]["date"] == "2026-11-28"

    cleared = client.patch(f"/api/v1/calendar/events/{event['id']}", headers=headers,
                           json={"time": ""})
    assert cleared.get_json()["data"]["time"] is None

    deleted = client.delete(f"/api/v1/calendar/events/{event['id']}", headers=headers)
    assert deleted.get_json()["data"]["deleted"] is True
    after = _get(client, headers, "?from=2026-11-01&to=2026-11-30")
    assert after["items"] == []


def test_event_date_validation(app, client):
    headers = _login(client)
    for payload in (
        {"title": "x", "date": "2026-02-30"},
        {"title": "x", "date": "2026/11/20"},
        {"title": "x", "date": "2026-11-20", "time": "25:00"},
        {"title": "x", "date": "2026-11-20", "category": "holiday"},
        {"title": "", "date": "2026-11-20"},
    ):
        resp = client.post("/api/v1/calendar/events", headers=headers, json=payload)
        assert resp.get_json()["code"] == 1001, (payload, resp.get_json())


def test_events_are_isolated_between_users(app, client):
    student_headers = _login(client)
    created = client.post("/api/v1/calendar/events", headers=student_headers, json={
        "title": "我的私人安排", "date": "2026-11-22",
    }).get_json()["data"]
    admin_headers = _login(client, "admin", "admin123")

    seen = _get(client, admin_headers, "?from=2026-11-01&to=2026-11-30")
    assert seen["items"] == []

    forbidden = client.patch(f"/api/v1/calendar/events/{created['id']}",
                             headers=admin_headers, json={"title": "改别人的"})
    assert forbidden.get_json()["code"] == 3002
    forbidden_delete = client.delete(
        f"/api/v1/calendar/events/{created['id']}", headers=admin_headers)
    assert forbidden_delete.get_json()["code"] == 3002


def test_calendar_requires_login(client):
    assert client.get("/api/v1/calendar").get_json()["code"] == 3001
    assert client.get("/api/v1/calendar/agenda").get_json()["code"] == 3001
    assert client.post("/api/v1/calendar/events", json={}).get_json()["code"] == 3001


# ---------------------------------------------------------------- 提醒
def test_agenda_splits_overdue_and_soon(app, client, base):
    today = datetime.now().date()
    overdue_day = datetime.combine(today - timedelta(days=3), datetime.min.time().replace(hour=9))
    soon_day = datetime.combine(today + timedelta(days=2), datetime.min.time().replace(hour=9))
    far_day = datetime.combine(today + timedelta(days=20), datetime.min.time().replace(hour=9))

    overdue = _task(app, base["plan_id"], "已经过期", due_at=overdue_day)
    soon = _task(app, base["plan_id"], "两天后到期", due_at=soon_day)
    far = _task(app, base["plan_id"], "二十天后", due_at=far_day)
    done = _task(app, base["plan_id"], "过期但已完成", due_at=overdue_day, status="done")

    headers = _login(client)
    agenda = client.get("/api/v1/calendar/agenda?days=7", headers=headers).get_json()["data"]

    assert [it["id"] for it in agenda["overdue"]] == [f"task:{overdue}"]
    assert [it["id"] for it in agenda["soon"]] == [f"task:{soon}"]
    assert f"task:{far}" not in [it["id"] for it in agenda["overdue"] + agenda["soon"]]
    assert f"task:{done}" not in [it["id"] for it in agenda["overdue"]]
    assert agenda["counts"] == {
        "overdue": 1, "soon": 1, "undated": 0, "undated_year_missing": 0,
    }


def test_agenda_includes_personal_events(app, client):
    headers = _login(client)
    today = datetime.now().date()
    client.post("/api/v1/calendar/events", headers=headers, json={
        "title": "三天后交材料", "date": (today + timedelta(days=3)).isoformat(),
    })
    client.post("/api/v1/calendar/events", headers=headers, json={
        "title": "上周的事", "date": (today - timedelta(days=7)).isoformat(),
    })
    agenda = client.get("/api/v1/calendar/agenda?days=7", headers=headers).get_json()["data"]
    assert [it["title"] for it in agenda["soon"]] == ["三天后交材料"]
    assert [it["title"] for it in agenda["overdue"]] == ["上周的事"]


def test_agenda_collects_year_missing_tasks(app, client, base):
    _task(app, base["plan_id"], "缺年份的截止",
          note="政策原文为「9 月 10 日」，但未给出年份；请确认具体年份后补填截止日期。")
    _task(app, base["plan_id"], "原文没日期",
          note="政策原文未给出具体日期，请自行确认截止时间。")
    headers = _login(client)
    agenda = client.get("/api/v1/calendar/agenda?days=7", headers=headers).get_json()["data"]
    assert len(agenda["undated"]) == 1
    assert agenda["undated"][0]["title"] == "缺年份的截止"
    assert agenda["counts"]["undated"] == 2
    assert agenda["counts"]["undated_year_missing"] == 1


def test_agenda_days_validation(app, client):
    headers = _login(client)
    assert client.get("/api/v1/calendar/agenda?days=0", headers=headers).get_json()["code"] == 1001
    assert client.get("/api/v1/calendar/agenda?days=61", headers=headers).get_json()["code"] == 1001
    assert client.get("/api/v1/calendar/agenda?days=x", headers=headers).get_json()["code"] == 1001
