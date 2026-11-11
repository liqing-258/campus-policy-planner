"""任务统计测试（契约 #29，M6 新增接口）。

统计口径是最容易被"看起来对"骗到的地方：数字对不上时没人知道是前端算错
还是后端算错。所以每个口径都单独一条用例，把边界（已完成/已取消算不算逾期、
分母含不含作废任务）钉死。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.contract import new_ulid
from app.models import ActionPlan, Task, User, db


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture()
def seeded_stats(app, seeded_policy):
    """一份已确认计划 + 一份草稿 + 5 条覆盖各种边界状态的任务。"""
    document_id = seeded_policy
    with app.app_context():
        user_id = db.session.execute(
            db.select(User.id).where(User.username == "student")).scalar_one()
        confirmed = ActionPlan(id=new_ulid(), user_id=user_id, document_id=document_id,
                               title="已确认计划", status="confirmed")
        draft = ActionPlan(id=new_ulid(), user_id=user_id, document_id=document_id,
                           title="草稿计划", status="draft")
        db.session.add_all([confirmed, draft])
        db.session.flush()

        now = _now()
        rows = [
            # 逾期未办：应计入 overdue
            ("逾期待办", "todo", "P0", now - timedelta(days=2)),
            # 七天内到期：应计入 due_soon
            ("本周到期", "todo", "P1", now + timedelta(days=3)),
            # 已完成但日期已过：**不算逾期**（做完了就不叫拖）
            ("早已完成", "done", "P2", now - timedelta(days=30)),
            # 已取消且日期已过：也不计入逾期，且不进完成率分母
            ("已作废", "cancelled", "P2", now - timedelta(days=10)),
            # 进行中、无截止日期
            ("进行中", "doing", "P3", None),
        ]
        for title, status, priority, due_at in rows:
            db.session.add(Task(
                id=new_ulid(), plan_id=confirmed.id, user_id=user_id,
                title=title, priority=priority, status=status, due_at=due_at,
            ))
        db.session.commit()
    return {"document_id": document_id}


def stats_of(client, headers):
    body = client.get("/api/v1/tasks/stats", headers=headers).get_json()
    assert body["code"] == 0, body
    return body["data"]


def test_empty_account_returns_full_shape(client, student_headers):
    """没任务的账号也要返回完整结构：前端不该对缺失字段写 if。"""
    data = stats_of(client, student_headers)
    assert data["total"] == 0
    assert data["completion_rate"] == 0
    assert set(data["by_status"]) == {"todo", "doing", "done", "deferred", "cancelled"}
    assert set(data["by_priority"]) == {"P0", "P1", "P2", "P3"}
    assert set(data["plans"]) == {"draft", "confirmed", "archived"}


def test_status_and_priority_counts(app, client, student_headers, seeded_stats):
    data = stats_of(client, student_headers)
    assert data["total"] == 5
    assert data["by_status"] == {
        "todo": 2, "doing": 1, "done": 1, "deferred": 0, "cancelled": 1,
    }
    assert data["by_priority"] == {"P0": 1, "P1": 1, "P2": 2, "P3": 1}


def test_overdue_excludes_done_and_cancelled(app, client, student_headers, seeded_stats):
    """三条任务日期已过，但只有一条算逾期：做完了与作废的都不叫拖。"""
    data = stats_of(client, student_headers)
    assert data["overdue"] == 1
    assert data["due_soon"] == 1
    assert data["open"] == 3


def test_completion_rate_excludes_cancelled(app, client, student_headers, seeded_stats):
    """1 done / (2 todo + 1 doing + 1 done) = 0.25；作废那条不进分母。"""
    data = stats_of(client, student_headers)
    assert data["completion_rate"] == pytest.approx(0.25)


def test_plan_counts_follow_status(app, client, student_headers, seeded_stats):
    data = stats_of(client, student_headers)
    assert data["plans"] == {"draft": 1, "confirmed": 1, "archived": 0}


def test_stats_are_scoped_to_the_current_user(app, client, student_headers,
                                              admin_headers, seeded_stats):
    """统计只算自己的：跨用户汇总会让"我的待办"变成"大家的待办"。"""
    assert stats_of(client, student_headers)["total"] == 5
    assert stats_of(client, admin_headers)["total"] == 0


def test_stats_requires_login(client):
    resp = client.get("/api/v1/tasks/stats")
    assert resp.status_code == 401
    assert resp.get_json()["code"] == 3001


def test_stats_reflects_a_transition_immediately(app, client, student_headers,
                                                 seeded_stats):
    """统计不是缓存：状态改完再查，数字必须立刻变。"""
    body = client.get("/api/v1/tasks?status=todo", headers=student_headers).get_json()
    task_id = body["data"]["items"][0]["id"]

    patched = client.post(f"/api/v1/tasks/{task_id}/status",
                          headers=student_headers, json={"status": "done"}).get_json()
    assert patched["code"] == 0, patched

    data = stats_of(client, student_headers)
    assert data["by_status"]["done"] == 2
    assert data["by_status"]["todo"] == 1
    assert data["completion_rate"] == pytest.approx(0.5)
