"""M5 计划与任务测试（T-16 / T-17 / T-18 / T-19 / T-20 / T-21）。

这四条验收用例的共同点：**它们验的都是"该拒绝的时候拒绝了没有"**，
而不是"计划生成得漂不漂亮"。所以断言集中在状态与错误码上：

- T-16 没有来源日期的任务 → `due_at=null` + `needs_human_confirmation=true`，不许编日期
- T-17 依赖成环 → 409 / 2006，且指出环在哪
- T-18 未确认的草稿 → 任务表里查不到（草稿只在返回里，不落库）
- T-19 重复确认 → 幂等，同一 plan_id，任务不重复插入
- T-20 非法状态转移 → 409 / 2005，消息里带当前状态与允许目标
- T-21 别人的任务 → 403 / 3002
"""

from __future__ import annotations

from app.contract import new_ulid

DRAFT = "/api/v1/plans/draft"


def draft(client, headers, document_id):
    resp = client.post(DRAFT, headers=headers, json={"document_id": document_id})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()["data"]


def confirm(client, headers, plan_id, tasks, title=None):
    body = {"tasks": tasks}
    if title:
        body["title"] = title
    return client.post(f"/api/v1/plans/{plan_id}/confirm", headers=headers, json=body)


def simple_tasks():
    return [
        {"temp_id": "T1", "title": "下载并填写申请表", "priority": "P0", "dependency_ids": []},
        {"temp_id": "T2", "title": "到教务办开具成绩单", "priority": "P1", "dependency_ids": ["T1"]},
    ]


def task_total(client, headers):
    return client.get("/api/v1/tasks", headers=headers).get_json()["data"]["total"]


# ============================================================ 草稿
def test_draft_returns_tasks_without_persisting_them(client, student_headers, policy):
    """T-18：草稿不落库 —— 任务表必须在确认前保持为空。"""
    before = task_total(client, student_headers)
    data = draft(client, student_headers, policy["document_id"])

    assert data["status"] == "draft"
    assert data["tasks"], "草稿至少要给出任务建议"
    assert data["plan_id"]
    assert task_total(client, student_headers) == before


def test_draft_task_without_source_date_has_null_due(client, student_headers, policy):
    """T-16：规则里没有日期就不许编日期，宁可留空并要求补充。"""
    data = draft(client, student_headers, policy["document_id"])
    by_rule = {task.get("source_rule_code"): task for task in data["tasks"]}

    payout = by_rule["R-PAYOUT"]           # 夹具里这条 deadline 规则没有具体日期
    assert payout["due_at"] is None
    assert payout["needs_human_confirmation"] is True

    deadline = by_rule["R-DEADLINE"]       # 这条有 "2026-09-10"
    assert deadline["due_at"].startswith("2026-09-10")
    assert deadline["due_source"] == "rule"


def test_draft_marks_material_rule_as_top_priority(client, student_headers, policy):
    data = draft(client, student_headers, policy["document_id"])
    material = [t for t in data["tasks"] if t.get("source_rule_code") == "R-MATERIAL"]
    assert material and material[0]["priority"] == "P0"


def test_draft_unknown_document_returns_2002(client, student_headers):
    resp = client.post(DRAFT, headers=student_headers, json={"document_id": new_ulid()})
    assert resp.status_code == 404
    body = resp.get_json()
    assert body["code"] == 2002
    assert "文档不存在" in body["message"]


# ============================================================ 确认
def test_confirm_persists_tasks_and_plan(client, student_headers, policy):
    plan_id = draft(client, student_headers, policy["document_id"])["plan_id"]
    resp = confirm(client, student_headers, plan_id, simple_tasks())
    assert resp.status_code == 200, resp.get_data(as_text=True)

    data = resp.get_json()["data"]
    assert data["status"] == "confirmed"
    assert data["task_count"] == 2
    assert task_total(client, student_headers) == 2

    listed = client.get(f"/api/v1/tasks?plan_id={plan_id}",
                        headers=student_headers).get_json()["data"]
    assert listed["total"] == 2
    assert all(t["status"] == "todo" for t in listed["items"])


def test_confirm_with_dependency_cycle_returns_2006(client, student_headers, policy):
    """T-17：环必须被拓扑排序拦下，并指出是哪几个任务成环。"""
    plan_id = draft(client, student_headers, policy["document_id"])["plan_id"]
    tasks = [
        {"temp_id": "T1", "title": "A", "dependency_ids": ["T2"]},
        {"temp_id": "T2", "title": "B", "dependency_ids": ["T1"]},
    ]
    resp = confirm(client, student_headers, plan_id, tasks)
    assert resp.status_code == 409
    body = resp.get_json()
    assert body["code"] == 2006
    assert "T1" in body["message"] and "T2" in body["message"]
    assert task_total(client, student_headers) == 0


def test_confirm_with_unknown_dependency_returns_1003(client, student_headers, policy):
    plan_id = draft(client, student_headers, policy["document_id"])["plan_id"]
    tasks = [{"temp_id": "T1", "title": "A", "dependency_ids": ["T9"]}]
    resp = confirm(client, student_headers, plan_id, tasks)
    assert resp.status_code == 422
    assert resp.get_json()["code"] == 1003


def test_confirm_with_empty_tasks_returns_1003(client, student_headers, policy):
    plan_id = draft(client, student_headers, policy["document_id"])["plan_id"]
    resp = confirm(client, student_headers, plan_id, [])
    assert resp.status_code == 422
    assert resp.get_json()["code"] == 1003


def test_confirm_is_idempotent(client, student_headers, policy):
    """T-19：重复确认返回同一个 plan_id，任务不翻倍。"""
    plan_id = draft(client, student_headers, policy["document_id"])["plan_id"]
    first = confirm(client, student_headers, plan_id, simple_tasks()).get_json()["data"]
    second = confirm(client, student_headers, plan_id, simple_tasks()).get_json()["data"]

    assert second["plan_id"] == first["plan_id"] == plan_id
    assert second["already_confirmed"] is True
    assert second["task_count"] == first["task_count"] == 2
    assert task_total(client, student_headers) == 2


def test_confirm_unknown_plan_returns_2004(client, student_headers):
    resp = confirm(client, student_headers, new_ulid(), simple_tasks())
    assert resp.status_code == 404
    body = resp.get_json()
    assert body["code"] == 2004
    assert "计划不存在" in body["message"]


def test_confirm_other_users_plan_is_forbidden(client, student_headers, admin_headers, policy):
    plan_id = draft(client, admin_headers, policy["document_id"])["plan_id"]
    resp = confirm(client, student_headers, plan_id, simple_tasks())
    assert resp.status_code == 403
    assert resp.get_json()["code"] == 3002


# ============================================================ 任务状态机
def _confirmed_task(client, headers, policy):
    plan_id = draft(client, headers, policy["document_id"])["plan_id"]
    confirm(client, headers, plan_id, simple_tasks()[:1])
    return client.get(f"/api/v1/tasks?plan_id={plan_id}",
                      headers=headers).get_json()["data"]["items"][0]


def test_status_transition_done_to_doing_is_rejected(client, student_headers, policy):
    """T-20：done → doing 不是合法转移（撤销要回 todo，不是回 doing）。"""
    task = _confirmed_task(client, student_headers, policy)

    ok = client.post(f"/api/v1/tasks/{task['id']}/status", headers=student_headers,
                     json={"status": "done"})
    assert ok.status_code == 200

    bad = client.post(f"/api/v1/tasks/{task['id']}/status", headers=student_headers,
                      json={"status": "doing"})
    assert bad.status_code == 409
    body = bad.get_json()
    assert body["code"] == 2005
    # 消息要能指导操作：说清当前状态和允许去哪
    assert "done" in body["message"] and "todo" in body["message"]


def test_status_done_sets_completed_at_and_undo_clears_it(client, student_headers, policy):
    task = _confirmed_task(client, student_headers, policy)

    done = client.post(f"/api/v1/tasks/{task['id']}/status", headers=student_headers,
                       json={"status": "done"}).get_json()["data"]
    assert done["completed_at"]

    undo = client.post(f"/api/v1/tasks/{task['id']}/status", headers=student_headers,
                       json={"status": "todo"}).get_json()["data"]
    assert undo["completed_at"] is None


def test_same_status_is_idempotent(client, student_headers, policy):
    task = _confirmed_task(client, student_headers, policy)
    resp = client.post(f"/api/v1/tasks/{task['id']}/status", headers=student_headers,
                       json={"status": "todo"})
    assert resp.status_code == 200
    assert resp.get_json()["data"]["status"] == "todo"


def test_invalid_status_value_is_rejected(client, student_headers, policy):
    task = _confirmed_task(client, student_headers, policy)
    resp = client.post(f"/api/v1/tasks/{task['id']}/status", headers=student_headers,
                       json={"status": "finished"})
    assert resp.status_code == 400
    assert resp.get_json()["code"] == 1001


# ============================================================ 归属
def test_other_users_task_is_invisible_and_forbidden(client, student_headers, admin_headers, policy):
    """T-21：别人的任务既不在列表里，也不能直接改。"""
    admin_task = _confirmed_task(client, admin_headers, policy)

    listed = client.get("/api/v1/tasks", headers=student_headers).get_json()["data"]
    assert admin_task["id"] not in [t["id"] for t in listed["items"]]

    resp = client.post(f"/api/v1/tasks/{admin_task['id']}/status",
                       headers=student_headers, json={"status": "done"})
    assert resp.status_code == 403
    assert resp.get_json()["code"] == 3002


def test_manual_task_requires_existing_plan(client, student_headers):
    resp = client.post("/api/v1/tasks", headers=student_headers,
                       json={"plan_id": new_ulid(), "title": "临时任务"})
    assert resp.status_code == 404
    body = resp.get_json()
    assert body["code"] == 2004
    assert "计划不存在" in body["message"]


def test_manual_task_created_under_own_plan(client, student_headers, policy):
    plan_id = draft(client, student_headers, policy["document_id"])["plan_id"]
    confirm(client, student_headers, plan_id, simple_tasks())

    resp = client.post("/api/v1/tasks", headers=student_headers,
                       json={"plan_id": plan_id, "title": "补交照片", "priority": "P3"})
    assert resp.status_code == 201, resp.get_data(as_text=True)
    assert resp.get_json()["data"]["status"] == "todo"
    assert task_total(client, student_headers) == 3


# ============================================================ 计划列表与编辑
def test_plan_patch_updates_title(client, student_headers, policy):
    plan_id = draft(client, student_headers, policy["document_id"])["plan_id"]
    resp = client.patch(f"/api/v1/plans/{plan_id}", headers=student_headers,
                        json={"title": "奖学金申请（改名后）"})
    assert resp.status_code == 200
    assert resp.get_json()["data"]["title"] == "奖学金申请（改名后）"


def test_plan_archive_after_confirm(client, student_headers, policy):
    plan_id = draft(client, student_headers, policy["document_id"])["plan_id"]
    confirm(client, student_headers, plan_id, simple_tasks())
    resp = client.patch(f"/api/v1/plans/{plan_id}", headers=student_headers,
                        json={"status": "archived"})
    assert resp.status_code == 200
    assert resp.get_json()["data"]["status"] == "archived"


def test_plan_cannot_jump_to_confirmed_via_patch(client, student_headers, policy):
    """确认只能走 /confirm：那条路径才会校验依赖与幂等。"""
    plan_id = draft(client, student_headers, policy["document_id"])["plan_id"]
    resp = client.patch(f"/api/v1/plans/{plan_id}", headers=student_headers,
                        json={"status": "confirmed"})
    assert resp.status_code in (400, 409)
    assert resp.get_json()["code"] in (1001, 2005)
