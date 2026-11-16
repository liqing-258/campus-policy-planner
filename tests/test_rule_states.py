# -*- coding: utf-8 -*-
"""R20：文件级「资格匹配准备度」与不适用标记。

**这个文件守的是三件在 R20 之前不存在的能力**

1. `0 条已复核条件` 要能分清五种处境（没抽过 / 抽失败 / 抽了没复核 / 抽了没抽到 /
   本来就不适用）—— 在此之前它们在前端全是同一个 "0 条"。
2. 抽取失败必须留痕。失败不留痕，管理员会反复点同一个按钮，而界面永远显示"0 条"。
3. 「不适用于资格匹配」是**人的判定**，不是推导结果：它要落库、要留理由、
   还要拦住自相矛盾的写入（已有已复核资格条件却标不适用）。

测试不调真模型：抽取相关用例把 `llm.chat_json` 换掉（`test_rules.py` 已覆盖
"真调模型"的路径），这里验的是**失败之后系统记了什么、界面据此显示什么**。
"""

from __future__ import annotations

import pytest

from app import rule_states
from app.contract import new_ulid
from app.errors import LlmOutputInvalid
from app.models import (
    AuditLog, Document, DocumentChunk, DocumentRuleState, PolicyRule, User, db,
)
from shared.enums import (
    APPLICABILITY_NOT_APPLICABLE,
    APPLICABILITY_UNCLASSIFIED,
    ATTEMPT_FAILED,
    ATTEMPT_SUCCEEDED,
    RULE_STATE_EXTRACT_FAILED,
    RULE_STATE_NO_CONDITIONS,
    RULE_STATE_NOT_APPLICABLE,
    RULE_STATE_PENDING_EXTRACT,
    RULE_STATE_PENDING_REVIEW,
    RULE_STATE_READY,
)


# ---------------------------------------------------------------- 夹具
def _doc(app, title: str, *, chunks: int = 2, parse_status: str = "indexed") -> str:
    with app.app_context():
        uploader = db.session.execute(
            db.select(User.id).where(User.username == "admin")
        ).scalar()
        doc = Document(
            id=new_ulid(), title=title, file_name=f"{title}.txt",
            stored_name=f"{title}.txt", file_type="txt", file_size=100,
            parse_status=parse_status, chunk_count=chunks, uploader_id=uploader,
        )
        db.session.add(doc)
        for index in range(chunks):
            db.session.add(DocumentChunk(
                id=new_ulid(), document_id=doc.id, chunk_index=index,
                content=f"第{index + 1}条 家庭经济困难学生可以申请；平均学分绩点不低于 3.5。",
                char_start=index * 100, char_end=(index + 1) * 100,
                page_number=index + 1, section_title="第一章 申请条件",
            ))
        db.session.commit()
        return doc.id


def _rule(app, doc_id: str, *, code: str, rule_type: str = "eligibility",
          field_key: str | None = "gpa", operator: str | None = "gte",
          expected: str | None = "3.5", review: str = "needs_review",
          extraction: str = "suggested") -> str:
    with app.app_context():
        chunk_id = db.session.execute(
            db.select(DocumentChunk.id).where(DocumentChunk.document_id == doc_id)
            .order_by(DocumentChunk.chunk_index)
        ).scalars().first()
        rule = PolicyRule(
            id=new_ulid(), document_id=doc_id, chunk_id=chunk_id, rule_code=code,
            rule_type=rule_type, description=f"测试规则 {code}", field_key=field_key,
            operator=operator, expected_value=expected, original_quote="家庭经济困难学生可以申请",
            quote_verified=1, extraction_status=extraction, review_status=review,
        )
        db.session.add(rule)
        db.session.commit()
        return rule.id


def _summary(app, doc_id: str) -> dict:
    with app.app_context():
        return rule_states.summary_of(doc_id)


# ============================================================ 1. 状态推导
@pytest.mark.parametrize(
    "approved,total,applicability,last_status,expected",
    [
        # 有已复核资格条件 → 能匹配（压过一切）
        (2, 5, APPLICABILITY_UNCLASSIFIED, ATTEMPT_SUCCEEDED, RULE_STATE_READY),
        # 标了"不适用"但已有已复核条件：数据说话，仍算 ready（另由 conflict 暴露矛盾）
        (2, 5, APPLICABILITY_NOT_APPLICABLE, ATTEMPT_SUCCEEDED, RULE_STATE_READY),
        # 有规则、没已复核条件 → 待审核
        (0, 5, APPLICABILITY_UNCLASSIFIED, ATTEMPT_SUCCEEDED, RULE_STATE_PENDING_REVIEW),
        # 人工判定不适用 → 不适用（优先于"待审核"）
        (0, 5, APPLICABILITY_NOT_APPLICABLE, ATTEMPT_SUCCEEDED, RULE_STATE_NOT_APPLICABLE),
        # 没规则 + 上次失败 → 提取失败
        (0, 0, APPLICABILITY_UNCLASSIFIED, ATTEMPT_FAILED, RULE_STATE_EXTRACT_FAILED),
        # 没规则 + 上次成功但 0 条 → 未发现资格条件（≠ 没抽过）
        (0, 0, APPLICABILITY_UNCLASSIFIED, ATTEMPT_SUCCEEDED, RULE_STATE_NO_CONDITIONS),
        # 一次都没跑过 → 待提取
        (0, 0, APPLICABILITY_UNCLASSIFIED, None, RULE_STATE_PENDING_EXTRACT),
    ],
)
def test_derive_state_matrix(approved, total, applicability, last_status, expected):
    """状态优先级是有讲究的：ready 压过"不适用"标签、不适用压过待审核、
    失败/成功但空 压过待提取。任何一处换位都会让界面撒谎。"""
    assert rule_states.derive_state(
        approved_conditions=approved, total_rules=total,
        applicability=applicability, last_status=last_status,
    ) == expected


# ============================================================ 2. 批量汇总
def test_summary_counts_only_condition_types_for_matching(app, student_headers):
    """`approved_conditions` 只数 eligibility/restriction：材料与时间节点
    通过复核不等于"可以匹配"，把它们算进去会让 0 条件的文件看起来可用。"""
    doc = _doc(app, "（R20）混合规则")
    _rule(app, doc, code="R-1", rule_type="eligibility", review="approved")
    _rule(app, doc, code="R-2", rule_type="restriction", review="approved")
    _rule(app, doc, code="R-3", rule_type="material", review="approved",
          field_key=None, operator=None, expected=None)
    _rule(app, doc, code="R-4", rule_type="deadline", review="approved",
          field_key=None, operator=None, expected=None)
    _rule(app, doc, code="R-5", rule_type="eligibility", review="needs_review")

    s = _summary(app, doc)
    assert s["total_rules"] == 5
    assert s["approved_rules"] == 4          # 复核通过的一共 4 条
    assert s["approved_conditions"] == 2     # 其中只有 2 条是资格条件
    assert s["pending_review"] == 1
    assert s["state"] == RULE_STATE_READY


def test_summary_exposes_target_groups_per_document(app):
    """奖项下拉按文件隔离：A 文件的奖项不能出现在 B 文件的 summary 里。"""
    a = _doc(app, "（R20）文件甲")
    b = _doc(app, "（R20）文件乙")
    _rule(app, a, code="R-1", review="approved")
    with app.app_context():
        rule = db.session.execute(
            db.select(PolicyRule).where(PolicyRule.document_id == a)
        ).scalars().first()
        rule.target_group = "国家励志奖学金申请人"
        db.session.commit()
    _rule(app, b, code="R-1", review="approved")
    with app.app_context():
        rule = db.session.execute(
            db.select(PolicyRule).where(PolicyRule.document_id == b)
        ).scalars().first()
        rule.target_group = "校级优秀学生奖学金申请人"
        db.session.commit()

    with app.app_context():
        both = rule_states.summaries_for([a, b])
    assert both[a]["target_groups"] == ["国家励志奖学金申请人"]
    assert both[b]["target_groups"] == ["校级优秀学生奖学金申请人"]


def test_summary_conflict_flag_when_label_contradicts_data(app):
    """标了"不适用"却又有已复核资格条件 → 必须置 conflict。
    匹配是真的会跑，界面不能只按"不适用"显示。"""
    doc = _doc(app, "（R20）矛盾标记")
    _rule(app, doc, code="R-1", review="approved")
    with app.app_context():
        db.session.add(DocumentRuleState(
            id=new_ulid(), document_id=doc,
            applicability=APPLICABILITY_NOT_APPLICABLE, applicability_note="原先判定不适用",
        ))
        db.session.commit()
    s = _summary(app, doc)
    assert s["state"] == RULE_STATE_READY
    assert s["conflict"] is True


def test_documents_list_carries_rule_summary(client, student_headers, app):
    """文档列表要带 rule_summary——前端就是靠它一次拿到 18 份文件的状态，
    而不是逐份请求（R20 之前的 N+1）。"""
    doc = _doc(app, "（R20）列表带状态")
    resp = client.get("/api/v1/documents?page=1&page_size=50", headers=student_headers)
    assert resp.get_json()["code"] == 0
    row = next(d for d in resp.get_json()["data"]["items"] if d["id"] == doc)
    assert "rule_summary" in row
    assert row["rule_summary"]["state"] == RULE_STATE_PENDING_EXTRACT

    detail = client.get(f"/api/v1/documents/{doc}", headers=student_headers)
    assert detail.get_json()["data"]["rule_summary"]["state"] == RULE_STATE_PENDING_EXTRACT


# ============================================================ 3. 不适用标记
def test_mark_not_applicable_requires_reason(client, admin_headers, app):
    doc = _doc(app, "（R20）服务指南")
    resp = client.patch(
        f"/api/v1/documents/{doc}/rule-applicability",
        headers=admin_headers, json={"applicability": "not_applicable"},
    )
    body = resp.get_json()
    # SchemaInvalid → 1003（1001 是请求载荷校验、1002 是分页越界，别混）
    assert body["code"] == 1003
    assert "理由" in body["message"]
    assert _summary(app, doc)["state"] == RULE_STATE_PENDING_EXTRACT


def test_mark_not_applicable_is_rejected_when_conditions_exist(client, admin_headers, app):
    """已有已复核资格条件却标"不适用"= 与事实冲突（匹配照样会跑），必须拒绝。"""
    doc = _doc(app, "（R20）已有条件")
    _rule(app, doc, code="R-1", review="approved")
    resp = client.patch(
        f"/api/v1/documents/{doc}/rule-applicability",
        headers=admin_headers,
        json={"applicability": "not_applicable", "note": "我觉得不该匹配"},
    )
    body = resp.get_json()
    assert body["code"] == 1003
    assert "已复核" in body["message"]
    assert _summary(app, doc)["state"] == RULE_STATE_READY


def test_mark_not_applicable_succeeds_and_is_audited(client, admin_headers, app):
    doc = _doc(app, "（R20）违规办法")
    resp = client.patch(
        f"/api/v1/documents/{doc}/rule-applicability",
        headers=admin_headers,
        json={"applicability": "not_applicable", "note": "全文为处分程序，无申请资格条件"},
    )
    body = resp.get_json()
    assert body["code"] == 0
    assert body["data"]["summary"]["state"] == RULE_STATE_NOT_APPLICABLE
    assert body["data"]["summary"]["applicability_note"].startswith("全文为处分程序")

    with app.app_context():
        audits = db.session.execute(
            db.select(AuditLog).where(AuditLog.action == "document.rule-applicability")
        ).scalars().all()
    assert len(audits) == 1
    assert audits[0].resource_id == doc


def test_student_cannot_mark_applicability(client, student_headers, app):
    doc = _doc(app, "（R20）越权标记")
    resp = client.patch(
        f"/api/v1/documents/{doc}/rule-applicability",
        headers=student_headers,
        json={"applicability": "not_applicable", "note": "学生不该有这个权限"},
    )
    assert resp.get_json()["code"] == 3002


# ============================================================ 4. 抽取失败留痕
def test_extract_failure_is_recorded_as_extract_failed(client, admin_headers, app, monkeypatch):
    """失败不留痕 = 界面上"抽失败"和"没抽过"一样。这条守的就是那个洞。"""
    doc = _doc(app, "（R20）抽取会失败")

    def _boom(*args, **kwargs):
        raise LlmOutputInvalid("模型只输出了推理内容（reasoning_tokens=12000），正文为空",
                               retriable=False, budget_exhausted=True)

    monkeypatch.setattr("app.llm.is_configured", lambda *a, **k: True)
    monkeypatch.setattr("app.llm.chat_json", _boom)

    resp = client.post(f"/api/v1/documents/{doc}/rules/extract",
                       headers=admin_headers, json={})
    assert resp.get_json()["code"] == 4002
    s = _summary(app, doc)
    assert s["state"] == RULE_STATE_EXTRACT_FAILED
    assert s["last_attempt_status"] == ATTEMPT_FAILED
    assert "12000" in (s["last_attempt_message"] or "")


def test_extract_escalates_budget_once_then_succeeds(client, admin_headers, app, monkeypatch):
    """预算被推理吃光时，抽取应升一档重试一次（12000 → 16000），而不是直接失败。

    这守的是 R20 的根因：8 片段政策在 6000 下 100% 失败，管理员点了抽取也拿不到规则。
    """
    doc = _doc(app, "（R20）预算升档")
    calls: list[int] = []

    def _fake_chat_json(system, user, *, temperature=None, max_tokens=None):
        calls.append(max_tokens)

        class _R:
            model = "fake"
        if max_tokens < 16000:
            raise LlmOutputInvalid("推理把预算吃光了", retriable=False, budget_exhausted=True)
        return ({"rules": [{
            "chunk_index": 1, "rule_code": "R-1", "rule_type": "eligibility",
            "description": "平均学分绩点不低于 3.5", "field_key": "gpa",
            "operator": "gte", "expected_value": "3.5", "target_group": None,
            "required_information": None, "effective_period": None,
            "page_or_section": "第一章", "original_quote": "平均学分绩点不低于 3.5。",
            "confidence": "high",
        }]}, _R())

    monkeypatch.setattr("app.llm.is_configured", lambda *a, **k: True)
    monkeypatch.setattr("app.llm.chat_json", _fake_chat_json)

    resp = client.post(f"/api/v1/documents/{doc}/rules/extract",
                       headers=admin_headers, json={})
    body = resp.get_json()
    assert body["code"] == 0, body.get("message")
    assert calls == [12000, 16000]           # 先按基线，撞墙后升一档，只升一次
    assert body["data"]["stored"] == 1
    s = _summary(app, doc)
    assert s["state"] == RULE_STATE_PENDING_REVIEW
    assert s["last_attempt_status"] == ATTEMPT_SUCCEEDED


def test_extract_success_without_rules_is_no_conditions(client, admin_headers, app, monkeypatch):
    """抽成功但一条没抽到 → 未发现资格条件（不是"没抽过"，也不是"不适用"）。"""
    doc = _doc(app, "（R20）抽了但空")

    class _R:
        model = "fake"

    monkeypatch.setattr("app.llm.is_configured", lambda *a, **k: True)
    monkeypatch.setattr("app.llm.chat_json", lambda *a, **k: ({"rules": []}, _R()))

    resp = client.post(f"/api/v1/documents/{doc}/rules/extract",
                       headers=admin_headers, json={})
    assert resp.get_json()["code"] == 0
    s = _summary(app, doc)
    assert s["state"] == RULE_STATE_NO_CONDITIONS
    assert s["last_attempt_rules"] == 0
    assert "不适用于资格匹配" in (s["last_attempt_message"] or "")


# ============================================================ 5. 匹配时的提示
def test_match_message_for_not_applicable_document(client, student_headers, app):
    """不适用政策的 2008 必须说清"为什么"并带出判定理由，而不是一句通用报错。"""
    doc = _doc(app, "（R20）不适用匹配")
    with app.app_context():
        db.session.add(DocumentRuleState(
            id=new_ulid(), document_id=doc,
            applicability=APPLICABILITY_NOT_APPLICABLE,
            applicability_note="全文为服务时间与收费标准，不含申请资格",
        ))
        db.session.commit()

    resp = client.post("/api/v1/matches", headers=student_headers,
                       json={"document_id": doc, "profile": {"gpa": 3.5}})
    body = resp.get_json()
    assert body["code"] == 2008
    assert "不适用于资格匹配" in body["message"]
    assert "服务时间" in body["message"]        # 理由要带出来
    assert body["data"]["rule_state"] == RULE_STATE_NOT_APPLICABLE
    assert body["data"]["rule_summary"]["state"] == RULE_STATE_NOT_APPLICABLE


def test_match_message_for_pending_review_document(client, student_headers, app):
    doc = _doc(app, "（R20）待复核政策")
    _rule(app, doc, code="R-1", review="needs_review")
    resp = client.post("/api/v1/matches", headers=student_headers,
                       json={"document_id": doc, "profile": {"gpa": 3.5}})
    body = resp.get_json()
    assert body["code"] == 2008
    assert "已抽取 1 条规则" in body["message"]
    assert body["data"]["rule_state"] == RULE_STATE_PENDING_REVIEW


def test_match_isolation_across_documents(client, student_headers, app):
    """A 文件的已复核条件不能参与 B 文件的判定（R20 专项测试 T4.4 的回归版）。"""
    a = _doc(app, "（R20）隔离甲")
    b = _doc(app, "（R20）隔离乙")
    _rule(app, a, code="R-A", review="approved", expected="3.0")
    _rule(app, b, code="R-B", review="approved", expected="4.0")

    body = client.post("/api/v1/matches", headers=student_headers,
                       json={"document_id": a, "profile": {"gpa": 3.5}}).get_json()
    assert body["code"] == 0
    codes = {it["rule_code"] for it in body["data"]["items"]}
    assert codes == {"R-A"}

    body_b = client.post("/api/v1/matches", headers=student_headers,
                         json={"document_id": b, "profile": {"gpa": 3.5}}).get_json()
    assert body_b["code"] == 0
    assert {it["rule_code"] for it in body_b["data"]["items"]} == {"R-B"}
    # 同一画像、两份文件，结论必须相反（3.5 ≥ 3.0 但 < 4.0）
    assert body["data"]["items"][0]["status"] == "met"
    assert body_b["data"]["items"][0]["status"] == "not_met"
