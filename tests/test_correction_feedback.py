"""纠错反馈与反馈管理（R16）测试。

与 `test_feedback.py`（M6 答案评价）互补：那一份管"有用/没用"，这一份管"纠错"。

这一层最怕的不是 500，而是三件"看起来正常"的错：

- 纠错反馈被当成"建议"，真的改了 documents / policy_rules 的内容（学生反馈
  绝不能动正式政策数据）；
- 重复点"提交"刷出一排一模一样的记录（幂等应该是覆盖，不是新增）；
- 统计把"有帮助比例"算成占全体反馈（应该只在答案评价内部算，纠错反馈没有
  有帮助/没帮助）。

断言打在具体字段、具体状态、具体计数上，不满足"接口 200 且非空"。
"""

from __future__ import annotations

import json

import pytest

from app.contract import new_ulid
from app.models import (
    Conversation,
    CorrectionFeedback,
    Document,
    Message,
    QaFeedback,
    User,
    db,
)


def _login(client, username="student", password="student123") -> dict:
    resp = client.post("/api/v1/auth/login",
                       json={"username": username, "password": password})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return {"Authorization": f"Bearer {resp.get_json()['data']['token']}"}


def _student(app) -> User:
    return db.session.execute(
        db.select(User).where(User.username == "student")).scalars().first()


def _doc(app, title="本科生奖学金评审办法", effective="active") -> str:
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


def _qa_pair(app, question="国家奖学金什么时候申请？",
             answer="国家奖学金每年秋季学期评审。",
             citations=None) -> tuple[str, str]:
    """造一对 user + assistant 消息，返回 (user_id, assistant_id)。"""
    with app.app_context():
        conv = Conversation(id=new_ulid(), user_id=_student(app).id,
                            title="测试会话")
        db.session.add(conv)
        q = Message(id=new_ulid(), conversation_id=conv.id, role="user",
                    content=question)
        a = Message(id=new_ulid(), conversation_id=conv.id, role="assistant",
                    content=answer, citations_json=json.dumps(citations)
                    if citations else None)
        db.session.add_all([q, a])
        db.session.commit()
        return q.id, a.id


def _qa_feedback(app, assistant_id: str, *, helpful: int = 0,
                 reason_code: str | None = None, comment: str = "依据错了",
                 status: str = "pending") -> str:
    with app.app_context():
        row = QaFeedback(
            id=new_ulid(), message_id=assistant_id, user_id=_student(app).id,
            helpful=helpful, reason_code=reason_code, comment=comment,
            status=status,
        )
        db.session.add(row)
        db.session.commit()
        return row.id


def _correction(app, target_type="document", target_id=None, *,
                document_id=None, reason_code=None, comment="日期写错了",
                status="pending") -> str:
    with app.app_context():
        row = CorrectionFeedback(
            id=new_ulid(), user_id=_student(app).id,
            target_type=target_type, target_id=target_id or new_ulid(),
            document_id=document_id, reason_code=reason_code, comment=comment,
            status=status,
        )
        db.session.add(row)
        db.session.commit()
        return row.id


# ---------------------------------------------------------------- 学生端：纠错提交
def test_correction_create_and_fields(client, student_headers, app):
    doc = _doc(app)
    resp = client.post("/api/v1/feedback/corrections", headers=student_headers,
                       json={"target_type": "document", "target_id": doc,
                             "document_id": doc, "reason_code": "outdated_policy",
                             "comment": "这份政策的发布日期写错了"})
    assert resp.status_code == 201
    data = resp.get_json()["data"]
    assert data["updated"] is False
    assert data["reason_code"] == "outdated_policy"


def test_correction_comment_required(client, student_headers):
    resp = client.post("/api/v1/feedback/corrections", headers=student_headers,
                       json={"target_type": "document", "target_id": "x"})
    assert resp.get_json()["code"] == 1001


def test_correction_bad_target_type(client, student_headers):
    resp = client.post("/api/v1/feedback/corrections", headers=student_headers,
                       json={"target_type": "chatbot", "target_id": "x",
                             "comment": "错"})
    assert resp.get_json()["code"] == 1001


def test_correction_requires_login(client):
    resp = client.post("/api/v1/feedback/corrections",
                       json={"target_type": "document", "target_id": "x",
                             "comment": "错"})
    assert resp.get_json()["code"] == 3001


def test_correction_resubmit_overwrites(client, student_headers, app):
    doc = _doc(app)
    first = client.post("/api/v1/feedback/corrections", headers=student_headers,
                        json={"target_type": "document", "target_id": doc,
                              "comment": "第一版"})
    assert first.status_code == 201
    second = client.post("/api/v1/feedback/corrections", headers=student_headers,
                         json={"target_type": "document", "target_id": doc,
                               "comment": "第二版"})
    assert second.get_json()["data"]["updated"] is True
    with app.app_context():
        count = db.session.execute(
            db.select(db.func.count()).select_from(CorrectionFeedback)
        ).scalar()
    assert count == 1
    assert second.get_json()["data"]["comment"] == "第二版"


def test_correction_never_touches_documents(client, student_headers, app):
    """学生反馈不能改正式政策内容——这是需求四的硬约束。"""
    doc = _doc(app, title="原名")
    client.post("/api/v1/feedback/corrections", headers=student_headers,
                json={"target_type": "document", "target_id": doc,
                      "comment": "标题应该是另一个名字"})
    with app.app_context():
        d = db.session.get(Document, doc)
        assert d.title == "原名"
        assert d.effective_status == "active"


# ---------------------------------------------------------------- 管理员：列表
def test_admin_list_merges_both_kinds(client, admin_headers, app):
    _q, a = _qa_pair(app)
    _qa_feedback(app, a, helpful=0, reason_code="wrong_fact")
    _correction(app, target_type="document", comment="纠错一条")
    resp = client.get("/api/v1/admin/feedback", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert data["total"] == 2
    kinds = {item["kind"] for item in data["items"]}
    assert kinds == {"qa", "correction"}


def test_admin_list_qa_carries_context(client, admin_headers, app):
    q, a = _qa_pair(app, question="国家奖学金什么时候申请？",
                    answer="每年秋季学期评审。",
                    citations=[{"document_id": "d1", "document_title": "奖学金办法",
                                "snippet": "片段", "section_title": "第二章"}])
    _qa_feedback(app, a, helpful=0, reason_code="outdated_policy",
                 comment="过期了")
    resp = client.get("/api/v1/admin/feedback?kind=qa", headers=admin_headers)
    item = resp.get_json()["data"]["items"][0]
    assert item["context"]["question"] == "国家奖学金什么时候申请？"
    assert item["context"]["answer"] == "每年秋季学期评审。"
    assert item["context"]["citations"][0]["document_title"] == "奖学金办法"
    assert item["comment"] == "过期了"


def test_admin_list_kind_filter(client, admin_headers, app):
    _q, a = _qa_pair(app)
    _qa_feedback(app, a)
    _correction(app, comment="纠错")
    resp = client.get("/api/v1/admin/feedback?kind=correction",
                      headers=admin_headers)
    items = resp.get_json()["data"]["items"]
    assert len(items) == 1 and items[0]["kind"] == "correction"


def test_admin_list_status_filter(client, admin_headers, app):
    _q, a = _qa_pair(app)
    _qa_feedback(app, a, status="resolved")
    _correction(app, comment="待处理", status="pending")
    resp = client.get("/api/v1/admin/feedback?status=resolved",
                      headers=admin_headers)
    items = resp.get_json()["data"]["items"]
    assert len(items) == 1 and items[0]["status"] == "resolved"


def test_admin_list_sorting(client, admin_headers, app):
    _q, a = _qa_pair(app)
    _qa_feedback(app, a)
    _correction(app, comment="较新的一条")
    resp = client.get("/api/v1/admin/feedback?sort=-created_at",
                      headers=admin_headers)
    items = resp.get_json()["data"]["items"]
    times = [i["created_at"] for i in items]
    assert times == sorted(times, reverse=True)


def test_admin_list_forbidden_for_student(client, student_headers):
    resp = client.get("/api/v1/admin/feedback", headers=student_headers)
    assert resp.get_json()["code"] == 3002


# ---------------------------------------------------------------- 管理员：统计
def test_stats_counts(client, admin_headers, app):
    q, a = _qa_pair(app)
    _qa_feedback(app, a, helpful=1)  # 有帮助，无 reason
    q2, a2 = _qa_pair(app)
    _qa_feedback(app, a2, helpful=0, reason_code="wrong_fact")
    q3, a3 = _qa_pair(app)
    _qa_feedback(app, a3, helpful=0, reason_code="too_vague")
    _correction(app, reason_code="outdated_policy", comment="过期")
    _correction(app, comment="无原因纠错", status="resolved")

    resp = client.get("/api/v1/admin/feedback/stats", headers=admin_headers)
    d = resp.get_json()["data"]
    assert d["total"] == 5
    assert d["qa"]["helpful"] == 1 and d["qa"]["unhelpful"] == 2
    assert d["qa"]["helpful_rate"] == pytest.approx(1 / 3, abs=0.001)
    # 政策错误 = qa 里 wrong_fact(1) + 全部纠错(2) = 3
    assert d["policy_error_count"] == 3
    assert d["status"]["resolved"] == 1
    dist = {r["reason_code"]: r["count"] for r in d["reason_distribution"]}
    assert dist["wrong_fact"] == 1 and dist["too_vague"] == 1
    assert dist["outdated_policy"] == 1


def test_stats_forbidden_for_student(client, student_headers):
    resp = client.get("/api/v1/admin/feedback/stats", headers=student_headers)
    assert resp.get_json()["code"] == 3002


# ---------------------------------------------------------------- 管理员：更新
def test_update_status_and_note(client, admin_headers, app):
    q, a = _qa_pair(app)
    fid = _qa_feedback(app, a)
    resp = client.patch(f"/api/v1/admin/feedback/qa/{fid}",
                        headers=admin_headers,
                        json={"status": "resolved", "admin_note": "已更正元数据"})
    data = resp.get_json()["data"]
    assert data["status"] == "resolved"
    assert data["admin_note"] == "已更正元数据"
    assert data["resolved_at"] is not None


def test_update_correction_status(client, admin_headers, app):
    fid = _correction(app, comment="错")
    resp = client.patch(f"/api/v1/admin/feedback/correction/{fid}",
                        headers=admin_headers, json={"status": "processing"})
    assert resp.get_json()["data"]["status"] == "processing"
    with app.app_context():
        row = db.session.get(CorrectionFeedback, fid)
        assert row.status == "processing"
        assert row.resolved_at is None


def test_update_requires_status_or_note(client, admin_headers, app):
    fid = _correction(app, comment="错")
    resp = client.patch(f"/api/v1/admin/feedback/correction/{fid}",
                        headers=admin_headers, json={})
    assert resp.get_json()["code"] == 1001


def test_update_bad_status(client, admin_headers, app):
    fid = _correction(app, comment="错")
    resp = client.patch(f"/api/v1/admin/feedback/correction/{fid}",
                        headers=admin_headers, json={"status": "done"})
    assert resp.get_json()["code"] == 1001


def test_update_not_found(client, admin_headers):
    resp = client.patch("/api/v1/admin/feedback/qa/不存在",
                        headers=admin_headers, json={"status": "resolved"})
    assert resp.get_json()["code"] == 2012


def test_update_forbidden_for_student(client, student_headers, app):
    fid = _correction(app, comment="错")
    resp = client.patch(f"/api/v1/admin/feedback/correction/{fid}",
                        headers=student_headers, json={"status": "resolved"})
    assert resp.get_json()["code"] == 3002
