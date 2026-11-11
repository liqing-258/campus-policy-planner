"""M5 三态匹配测试（T-13 / T-14 / T-15）。

匹配**完全不调模型**（设计 §7.6 的硬边界）：资格判断必须可复现、可断言、可解释。
所以这个文件不需要 mock 任何东西 —— 它是纯函数级的行为断言。

三态的定义：
- `met`      字段齐全、算子可求值、比较通过
- `not_met`  字段齐全、算子可求值、比较不通过
- `unknown`  无法判定，且必须说明**为什么**（missing_info / ambiguous_rule / no_evidence）
"""

from __future__ import annotations

import pytest

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


# ============================================================ M6：按奖项过滤
@pytest.fixture()
def grouped_policy(app):
    """一份文件里同时写着两个奖项的条件——这正是 M5 那个缺口的现场。

    真实政策就是这样：同一份《奖学金评审办法》里，励志奖学金要求绩点 ≥ 3.0，
    优秀学生奖学金要求 ≥ 3.5。不区分奖项时，一个绩点 3.2 的励志申请人
    会被优秀奖学金那条判成"不符合"，而且理由看着还挺有理有据。
    """
    from app.models import Document, DocumentChunk, PolicyRule, User, db

    with app.app_context():
        uploader = db.session.execute(
            db.select(User.id).where(User.username == "admin")).scalar_one()
        document = Document(
            id=new_ulid(), title="（测试）奖学金评审办法", file_name="p.pdf",
            stored_name=f"{new_ulid()}.pdf", file_type="pdf", file_size=100,
            parse_status="indexed", chunk_count=1, uploader_id=uploader,
        )
        db.session.add(document)
        chunk = DocumentChunk(
            id=new_ulid(), document_id=document.id, chunk_index=0,
            content="励志奖学金要求绩点不低于 3.0；优秀学生奖学金要求不低于 3.5；"
                    "申请人须为在籍本科生。",
            char_start=0, char_end=60, page_number=1, section_title="第二章",
        )
        db.session.add(chunk)
        db.session.flush()
        for code, group, operator, expected, description in (
            ("R-CSL", "励志奖学金", "gte", "3.0", "励志奖学金绩点不低于 3.0"),
            ("R-YX", "优秀学生奖学金", "gte", "3.5", "优秀学生奖学金绩点不低于 3.5"),
            ("R-COMMON", None, "equals", "enrolled", "须为在籍在校学生"),
        ):
            db.session.add(PolicyRule(
                id=new_ulid(), document_id=document.id, chunk_id=chunk.id,
                rule_code=code, rule_type="eligibility", description=description,
                target_group=group, field_key=("gpa" if code != "R-COMMON"
                                               else "enrollment_status"),
                operator=operator, expected_value=expected,
                original_quote=description, quote_verified=1,
                extraction_status="suggested", review_status="approved",
                confidence="high", page_or_section="第二章",
            ))
        db.session.commit()
        return document.id


def _gpa_32(enrollment="enrolled"):
    """绩点 3.2 的申请人：够励志（≥3.0）、不够优秀（<3.5）。"""
    return {"gpa": 3.2, "enrollment_status": enrollment}


def test_without_target_group_both_awards_are_compared(client, student_headers,
                                                       grouped_policy):
    body = run_match(client, student_headers, grouped_policy, _gpa_32()).get_json()
    assert body["code"] == 0, body
    codes = set(items_by_code(body["data"]))
    assert codes == {"R-CSL", "R-YX", "R-COMMON"}
    assert body["data"]["filtered_out"] == 0


def test_target_group_keeps_the_right_award(client, student_headers, grouped_policy):
    """同一个绩点 3.2，申请励志奖学金应当是"符合"——M5 时它被判成不符合。"""
    data = client.post(MATCH, headers=student_headers, json={
        "document_id": grouped_policy, "profile": _gpa_32(),
        "target_group": "励志奖学金",
    }).get_json()["data"]

    assert data["target_group"] == "励志奖学金"
    assert data["filtered_out"] == 1
    items = items_by_code(data)
    assert set(items) == {"R-CSL", "R-COMMON"}
    assert items["R-CSL"]["status"] == "met"
    # 通用条件（未标注奖项）必须保留，否则"在籍"这类前提会被一起滤掉
    assert items["R-COMMON"]["status"] == "met"


def test_same_profile_fails_under_the_other_award(client, student_headers,
                                                  grouped_policy):
    """换成优秀学生奖学金，3.2 就是不符合。这不是矛盾，是两条不同的门槛。"""
    data = client.post(MATCH, headers=student_headers, json={
        "document_id": grouped_policy, "profile": _gpa_32(),
        "target_group": "优秀学生奖学金",
    }).get_json()["data"]
    items = items_by_code(data)
    assert set(items) == {"R-YX", "R-COMMON"}
    assert items["R-YX"]["status"] == "not_met"
    assert "3.2" in items["R-YX"]["rationale"] and "3.5" in items["R-YX"]["rationale"]


def test_unknown_target_group_says_what_is_available(client, student_headers,
                                                     grouped_policy):
    """选了一个文件里没有的奖项：不能返回"0 符合"，那会被读成"我全都不符合"。"""
    resp = client.post(MATCH, headers=student_headers, json={
        "document_id": grouped_policy, "profile": _gpa_32(),
        "target_group": "国家奖学金",
    })
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["code"] == 2008
    assert body["data"]["target_group"] == "国家奖学金"
    assert set(body["data"]["available_groups"]) == {"励志奖学金", "优秀学生奖学金"}

