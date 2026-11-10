"""M5 规则抽取测试（T-13–T-22 中与规则相关的部分 + N-01 契约校验闸）。

**不调真实模型**：monkeypatch `app.llm._post`，这样"模型返回什么"变成测试数据。
这里验的不是"模型抽得准不准"（那要靠人工标注），而是**模型乱输出时系统是否守得住**：

1. 引用原文不在片段里 → 丢弃该条（这是 §7.5 第 2 点的子串校验，不是可选项）
2. 算子 / 字段不在白名单 → 丢弃（防止模型发明 `family_income` 这类不该采集的字段）
3. chunk_index 越界 → 丢弃（与 M4 的引用越界同一思路：编号可比较，chunk_id 不可信）
4. 抽出来的规则一律 needs_review，绝不直接参与匹配
"""

from __future__ import annotations

import json

import pytest

from app.contract import new_ulid
from app.models import PolicyRule, db

EXTRACT = "/api/v1/documents/{}/rules/extract"


def fake_payload(content: str) -> dict:
    return {
        "model": "fake-model",
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 200, "completion_tokens": 80,
                  "completion_tokens_details": {"reasoning_tokens": 10}},
    }


def rules_json(*rules: dict) -> str:
    return json.dumps({"rules": list(rules)}, ensure_ascii=False)


def a_rule(**overrides) -> dict:
    base = {
        "chunk_index": 1,
        "rule_code": "R-EX-1",
        "rule_type": "eligibility",
        "description": "学年平均学分绩点不低于 3.0",
        "field_key": "gpa",
        "operator": "gte",
        "expected_value": "3.0",
        "original_quote": "学年平均学分绩点不低于 3.0",
        "confidence": "high",
    }
    base.update(overrides)
    return base


def configure_llm(app, *, key: str = "test-key", model: str = "test-model"):
    settings = app.extensions["settings"]
    settings.llm_api_key = key
    settings.llm_model = model


# ============================================================ 抽取：正常
def test_extract_stores_rules_as_needs_review(app, client, admin_headers, policy, monkeypatch):
    configure_llm(app)
    monkeypatch.setattr("app.llm._post", lambda body: (fake_payload(rules_json(a_rule())), 12))

    resp = client.post(EXTRACT.format(policy["document_id"]), headers=admin_headers, json={})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    data = resp.get_json()["data"]
    assert data["stored"] == 1
    assert data["dropped"] == []
    assert data["rules"][0]["review_status"] == "needs_review"
    assert data["rules"][0]["quote_verified"] is True

    with app.app_context():
        row = db.session.execute(
            db.select(PolicyRule).where(PolicyRule.rule_code == "R-EX-1")).scalar_one()
        # 抽取结果绝不直接进匹配：状态必须是待复核
        assert row.review_status == "needs_review"


# ============================================================ 抽取：契约校验闸
def test_extract_drops_rule_whose_quote_is_not_in_chunk(app, client, admin_headers, policy, monkeypatch):
    """模型编了一句原文里没有的话当依据 —— 这是最常见的幻觉形式。"""
    configure_llm(app)
    monkeypatch.setattr("app.llm._post", lambda body: (fake_payload(rules_json(
        a_rule(rule_code="R-FAKE", original_quote="申请人须年满十八周岁且无犯罪记录"),
    )), 10,))

    data = client.post(EXTRACT.format(policy["document_id"]),
                       headers=admin_headers, json={}).get_json()["data"]
    assert data["stored"] == 0
    assert len(data["dropped"]) == 1
    assert "原文" in data["dropped"][0]["reason"]

    with app.app_context():
        assert db.session.execute(
            db.select(PolicyRule).where(PolicyRule.rule_code == "R-FAKE")).scalar() is None


def test_extract_drops_unknown_operator(app, client, admin_headers, policy, monkeypatch):
    configure_llm(app)
    monkeypatch.setattr("app.llm._post", lambda body: (fake_payload(rules_json(
        a_rule(rule_code="R-OP", operator="approximately"),
    )), 10,))

    data = client.post(EXTRACT.format(policy["document_id"]),
                       headers=admin_headers, json={}).get_json()["data"]
    assert data["stored"] == 0
    assert "算子" in data["dropped"][0]["reason"]


def test_extract_drops_field_outside_whitelist(app, client, admin_headers, policy, monkeypatch):
    """`family_income` 不在 field_key 白名单里 —— 属于隐私红线，不是"字段名不规范"。"""
    configure_llm(app)
    monkeypatch.setattr("app.llm._post", lambda body: (fake_payload(rules_json(
        a_rule(rule_code="R-PRIVACY", field_key="family_income"),
    )), 10,))

    data = client.post(EXTRACT.format(policy["document_id"]),
                       headers=admin_headers, json={}).get_json()["data"]
    assert data["stored"] == 0
    assert "白名单" in data["dropped"][0]["reason"]

    with app.app_context():
        assert db.session.execute(
            db.select(PolicyRule).where(PolicyRule.rule_code == "R-PRIVACY")).scalar() is None


def test_extract_drops_out_of_range_chunk_index(app, client, admin_headers, policy, monkeypatch):
    configure_llm(app)
    monkeypatch.setattr("app.llm._post", lambda body: (fake_payload(rules_json(
        a_rule(rule_code="R-OOR", chunk_index=99),
    )), 10,))

    data = client.post(EXTRACT.format(policy["document_id"]),
                       headers=admin_headers, json={}).get_json()["data"]
    assert data["stored"] == 0
    assert "片段" in data["dropped"][0]["reason"]


def test_extract_rejects_non_json_output(app, client, admin_headers, policy, monkeypatch):
    """模型返回散文而不是 JSON —— 必须 4002，不能"尽力解析"成规则。"""
    configure_llm(app)
    monkeypatch.setattr("app.llm._post",
                        lambda body: (fake_payload("好的，我找到了三条规则：……"), 10))

    resp = client.post(EXTRACT.format(policy["document_id"]), headers=admin_headers, json={})
    assert resp.status_code == 502
    assert resp.get_json()["code"] == 4002


# ============================================================ 抽取：权限与降级
def test_extract_requires_admin(app, client, student_headers, policy):
    """T-22 的规则版：学生不能触发抽取（与"学生不能上传"同一条边界）。"""
    resp = client.post(EXTRACT.format(policy["document_id"]), headers=student_headers, json={})
    assert resp.status_code == 403
    assert resp.get_json()["code"] == 3002


def test_extract_without_key_returns_degraded(app, client, admin_headers, policy):
    resp = client.post(EXTRACT.format(policy["document_id"]), headers=admin_headers, json={})
    assert resp.status_code == 200
    assert resp.get_json()["code"] == 4006


def test_extract_unknown_document_returns_2002(app, client, admin_headers, monkeypatch):
    configure_llm(app)
    monkeypatch.setattr("app.llm._post", lambda body: (fake_payload(rules_json(a_rule())), 10))
    resp = client.post(EXTRACT.format(new_ulid()), headers=admin_headers, json={})
    assert resp.status_code == 404
    body = resp.get_json()
    assert body["code"] == 2002
    # 必须区分"文档不存在"与"接口不存在"：后者说明路由压根没注册，
    # 只断言 404/2002 的用例在实现之前就是绿的（本条最初就是这样假绿的）
    assert "文档不存在" in body["message"]


# ============================================================ 列表与复核
def test_list_rules_filters_by_review_status(client, student_headers, policy):
    data = client.get(f"/api/v1/documents/{policy['document_id']}/rules",
                      headers=student_headers).get_json()["data"]
    assert data["total"] == 8  # 夹具里 8 条全部 approved

    data = client.get(f"/api/v1/documents/{policy['document_id']}/rules?review_status=needs_review",
                      headers=student_headers).get_json()["data"]
    assert data["total"] == 0


def test_approve_requires_operator(app, client, admin_headers, policy):
    """复核通过一条没有算子的**条件类**规则 = 让一条无法求值的规则进匹配，必须拦住。

    注意不能用夹具里的 R-SUBJECTIVE：它有 `manual_review` 算子，
    正是"无法自动判定"的合法表达方式（T-14 靠它）。真正该拦的是
    "eligibility 类 + 没有任何算子"——那才是漏抽了算子。
    """
    from app.contract import new_ulid
    from app.models import PolicyRule, db

    with app.app_context():
        bare = PolicyRule(
            id=new_ulid(), document_id=policy["document_id"], chunk_id=policy["chunk_ids"][0],
            rule_code="R-BARE", rule_type="eligibility", description="学习成绩良好",
            original_quote="学习成绩良好", quote_verified=1,
            extraction_status="suggested", review_status="needs_review",
        )
        db.session.add(bare)
        db.session.commit()
        bare_id = bare.id

    resp = client.patch(f"/api/v1/rules/{bare_id}", headers=admin_headers,
                        json={"review_status": "approved"})
    assert resp.status_code == 422
    assert resp.get_json()["code"] == 1003


def test_patch_rule_rejects_unknown_operator(client, admin_headers, policy):
    rule_id = policy["rule_codes"]["R-GPA"]
    resp = client.patch(f"/api/v1/rules/{rule_id}", headers=admin_headers,
                        json={"operator": "about"})
    assert resp.status_code == 400
    assert resp.get_json()["code"] == 1001


def test_patch_rule_updates_expected_value(client, admin_headers, policy):
    rule_id = policy["rule_codes"]["R-GPA"]
    resp = client.patch(f"/api/v1/rules/{rule_id}", headers=admin_headers,
                        json={"expected_value": "2.8"})
    assert resp.status_code == 200
    assert resp.get_json()["data"]["expected_value"] == "2.8"


def test_patch_rule_not_found(client, admin_headers):
    resp = client.patch(f"/api/v1/rules/{new_ulid()}", headers=admin_headers,
                        json={"expected_value": "2.8"})
    assert resp.status_code == 404
    body = resp.get_json()
    assert body["code"] == 2003
    assert "规则不存在" in body["message"]


def test_patch_rule_requires_admin(client, student_headers, policy):
    resp = client.patch(f"/api/v1/rules/{policy['rule_codes']['R-GPA']}",
                        headers=student_headers, json={"expected_value": "2.8"})
    assert resp.status_code == 403
    assert resp.get_json()["code"] == 3002


@pytest.mark.parametrize("payload", [
    {"review_status": "maybe"},
    {"rule_type": "vibes"},
    {"unknown_field": 1},
])
def test_patch_rule_rejects_bad_payload(client, admin_headers, policy, payload):
    resp = client.patch(f"/api/v1/rules/{policy['rule_codes']['R-GPA']}",
                        headers=admin_headers, json=payload)
    assert resp.status_code in (400, 422)
    assert resp.get_json()["code"] in (1001, 1003)
