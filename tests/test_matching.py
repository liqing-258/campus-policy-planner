"""M5 三态匹配测试（T-13 / T-14 / T-15）。

匹配**完全不调模型**（设计 §7.6 的硬边界）：资格判断必须可复现、可断言、可解释。
所以这个文件不需要 mock 任何东西 —— 它是纯函数级的行为断言。

三态的定义：
- `met`      字段齐全、算子可求值、比较通过
- `not_met`  字段齐全、算子可求值、比较不通过
- `unknown`  无法判定，且必须说明**为什么**（missing_info / ambiguous_rule / no_evidence）
"""

from __future__ import annotations

from app.contract import new_ulid

MATCH = "/api/v1/matches"

# 一份"四项可判定 + 一项缺字段 + 一项主观"的用户画像
PROFILE = {
    "student_type": "full_time_undergraduate",
    "grade": 3,
    "gpa": 2.5,          # R-GPA 要求 >= 3.0 → not_met
    "rank_percent": 45,  # R-RANK 要求 <= 30 → not_met，且理由里要有 45 与 30
    "disciplinary_record": "none",
    "major": "计算机科学与技术",
    "credits_earned": 120,
    "enrollment_status": "enrolled",
    # 故意不给 poverty_certified → R-POVERTY 应为 unknown/missing_info
}


def run_match(client, headers, document_id, profile=None):
    return client.post(MATCH, headers=headers,
                       json={"document_id": document_id, "profile": profile or PROFILE})


def items_by_code(data):
    return {item["rule_code"]: item for item in data["items"]}


# ============================================================ T-13 缺字段
def test_missing_field_yields_unknown_missing_info(client, student_headers, policy):
    data = run_match(client, student_headers, policy["document_id"]).get_json()["data"]
    poverty = items_by_code(data)["R-POVERTY"]

    assert poverty["status"] == "unknown"
    assert poverty["unknown_reason"] == "missing_info"
    # 理由必须能让用户采取行动：说清缺哪个字段
    assert "poverty_certified" in poverty["rationale"]


# ============================================================ T-14 主观条款
def test_manual_review_operator_yields_ambiguous(client, student_headers, policy):
    data = run_match(client, student_headers, policy["document_id"]).get_json()["data"]
    subjective = items_by_code(data)["R-SUBJECTIVE"]

    assert subjective["status"] == "unknown"
    assert subjective["unknown_reason"] == "ambiguous_rule"
    # 主动放弃自动判定，而不是猜一个结论
    assert "人工" in subjective["rationale"] or "无法自动" in subjective["rationale"]


# ============================================================ T-15 算子求值
def test_rank_operator_not_met_reports_both_numbers(client, student_headers, policy):
    data = run_match(client, student_headers, policy["document_id"]).get_json()["data"]
    rank = items_by_code(data)["R-RANK"]

    assert rank["status"] == "not_met"
    assert rank["user_value"] == "45"
    assert rank["expected_value"] == "30"
    # "不符合"必须带具体数值，否则用户没法判断差多少
    assert "45" in rank["rationale"] and "30" in rank["rationale"]


def test_gpa_gte_not_met(client, student_headers, policy):
    data = run_match(client, student_headers, policy["document_id"]).get_json()["data"]
    gpa = items_by_code(data)["R-GPA"]
    assert gpa["status"] == "not_met"
    assert "2.5" in gpa["rationale"]


def test_in_operator_met(client, student_headers, policy):
    data = run_match(client, student_headers, policy["document_id"]).get_json()["data"]
    grade = items_by_code(data)["R-GRADE"]
    assert grade["status"] == "met"


def test_summary_counts_match_items(client, student_headers, policy):
    data = run_match(client, student_headers, policy["document_id"]).get_json()["data"]
    summary = data["summary"]

    # 只统计"条件类"规则：material / deadline 不参与资格判定，不该出现在 items 里
    assert summary["total"] == len(data["items"]) == 5
    assert summary["met"] == 1
    assert summary["not_met"] == 2
    assert summary["unknown"] == 2
    assert summary["met"] + summary["not_met"] + summary["unknown"] == summary["total"]
    assert data["disclaimer"], "必须带回免责说明：匹配结果是辅助判断，不构成资格认定"


def test_every_item_carries_evidence(client, student_headers, policy):
    data = run_match(client, student_headers, policy["document_id"]).get_json()["data"]
    for item in data["items"]:
        evidence = item["evidence"]
        assert evidence["chunk_id"] in policy["chunk_ids"]
        assert evidence["page"]
        assert evidence["quote"]


# ============================================================ 入参与边界
def test_profile_rejects_field_outside_whitelist(client, student_headers, policy):
    """画像里塞 `family_income` —— 隐私红线，不是"多余字段"。"""
    profile = dict(PROFILE, family_income=8000)
    resp = run_match(client, student_headers, policy["document_id"], profile)
    assert resp.status_code in (400, 422)
    assert resp.get_json()["code"] in (1001, 1003)


def test_match_unknown_document_returns_2002(client, student_headers):
    resp = run_match(client, student_headers, new_ulid())
    assert resp.status_code == 404
    body = resp.get_json()
    assert body["code"] == 2002
    assert "文档不存在" in body["message"]


def test_no_approved_rules_returns_business_state(app, client, student_headers, policy):
    """规则还没复核完就匹配 → 明确告诉用户"先复核规则"，而不是返回 0 符合 0 不符合。"""
    from app.models import PolicyRule, db

    with app.app_context():
        db.session.execute(
            db.update(PolicyRule).where(PolicyRule.document_id == policy["document_id"])
            .values(review_status="needs_review"))
        db.session.commit()

    resp = run_match(client, student_headers, policy["document_id"])
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["code"] == 2008
    assert body["data"]["approved_rules"] == 0


# ============================================================ 归属与持久化
def test_match_result_is_persisted_and_readable_by_owner(client, student_headers, policy):
    session_id = run_match(client, student_headers, policy["document_id"]).get_json()["data"]["session_id"]

    data = client.get(f"/api/v1/matches/{session_id}", headers=student_headers).get_json()["data"]
    assert data["session_id"] == session_id
    assert data["summary"]["total"] == 5


def test_other_users_match_is_forbidden(client, student_headers, admin_headers, policy):
    session_id = run_match(client, student_headers, policy["document_id"]).get_json()["data"]["session_id"]
    resp = client.get(f"/api/v1/matches/{session_id}", headers=admin_headers)
    assert resp.status_code == 403
    assert resp.get_json()["code"] == 3002


def test_unknown_match_returns_2002(client, student_headers):
    resp = client.get(f"/api/v1/matches/{new_ulid()}", headers=student_headers)
    assert resp.status_code == 404
    body = resp.get_json()
    assert body["code"] == 2002
    assert "匹配" in body["message"] and "接口不存在" not in body["message"]
