# -*- coding: utf-8 -*-
"""R21：资格匹配页只列「已有一条以上已复核资格条件」的政策。

**这个筛选是动态的，所以测试也必须验"进出"**，而不只是验"现在是 3 份"：
- 管理员复核通过一份新政策 → 它自动出现；
- 撤销某份政策的复核 → 它自动消失；
- 只有材料/时间节点通过复核 ≠ 可匹配（那不是"资格条件"）；
- 知识库接口不带这个参数，18 份一份不少。

为什么把判据放后端：可用性判据（approved + 条件类）本来就在后端，
前端按 `rule_summary` 再筛一遍会出现两份真相；而且分页总数也得按过滤后的集合算。
"""

from __future__ import annotations

import pytest

from app.contract import new_ulid
from app.models import Document, DocumentChunk, PolicyRule, User, db


def _doc(app, title: str, *, chunks: int = 2) -> str:
    with app.app_context():
        uploader = db.session.execute(
            db.select(User.id).where(User.username == "admin")
        ).scalar()
        doc = Document(
            id=new_ulid(), title=title, file_name=f"{title}.txt",
            stored_name=f"{title}.txt", file_type="txt", file_size=100,
            parse_status="indexed", chunk_count=chunks, uploader_id=uploader,
        )
        db.session.add(doc)
        for index in range(chunks):
            db.session.add(DocumentChunk(
                id=new_ulid(), document_id=doc.id, chunk_index=index,
                content=f"第{index + 1}条 平均学分绩点不低于 3.5。",
                char_start=index * 100, char_end=(index + 1) * 100,
                page_number=index + 1, section_title="第一章 申请条件",
            ))
        db.session.commit()
        return doc.id


def _rule(app, doc_id: str, *, rule_type: str = "eligibility",
          review: str = "approved", code: str | None = None) -> str:
    with app.app_context():
        chunk_id = db.session.execute(
            db.select(DocumentChunk.id).where(DocumentChunk.document_id == doc_id)
            .order_by(DocumentChunk.chunk_index)
        ).scalars().first()
        rule = PolicyRule(
            id=new_ulid(), document_id=doc_id, chunk_id=chunk_id,
            rule_code=code or f"R-{new_ulid()[:4]}", rule_type=rule_type,
            description="平均学分绩点不低于 3.5", field_key="gpa",
            operator="gte", expected_value="3.5",
            original_quote="平均学分绩点不低于 3.5。", quote_verified=1,
            extraction_status="verified" if review == "approved" else "suggested",
            review_status=review,
        )
        db.session.add(rule)
        db.session.commit()
        return rule.id


def _titles(client, headers, query: str = "") -> list[str]:
    resp = client.get(f"/api/v1/documents?page=1&page_size=100{query}", headers=headers)
    assert resp.get_json()["code"] == 0
    return [item["title"] for item in resp.get_json()["data"]["items"]]


# ============================================================ 1. 只列可匹配的
def test_match_ready_lists_only_approved_condition_documents(client, student_headers, app):
    ready = _doc(app, "（R21）可匹配政策")
    _rule(app, ready)
    pending = _doc(app, "（R21）只有待复核规则")
    _rule(app, pending, review="needs_review")
    empty = _doc(app, "（R21）一条规则都没有")
    material_only = _doc(app, "（R21）只有材料规则通过了")
    _rule(app, material_only, rule_type="material")

    titles = _titles(client, student_headers, "&match_ready=1")
    assert titles == ["（R21）可匹配政策"], titles
    assert "（R21）只有待复核规则" not in titles
    assert "（R21）一条规则都没有" not in titles
    # 材料类通过复核不算"资格条件"——它决定"要交什么"，不决定"符不符合"
    assert "（R21）只有材料规则通过了" not in titles
    assert empty  # 只为吃变量，避免 linter 抱怨


def test_match_ready_counts_both_condition_types(client, student_headers, app):
    """restriction 也是条件类（如"处分期内不得参评"），不能漏。"""
    eligible = _doc(app, "（R21）eligibility 通过")
    _rule(app, eligible, rule_type="eligibility")
    restricted = _doc(app, "（R21）restriction 通过")
    _rule(app, restricted, rule_type="restriction")

    titles = _titles(client, student_headers, "&match_ready=1")
    assert set(titles) == {"（R21）eligibility 通过", "（R21）restriction 通过"}


# ============================================================ 2. 动态进出
def test_newly_approved_document_enters_the_list(client, admin_headers, student_headers, app):
    """管理员复核通过一条资格条件后，该政策应**自动**出现在匹配列表里。"""
    doc = _doc(app, "（R21）新复核的政策")
    rule_id = _rule(app, doc, review="needs_review")

    assert "（R21）新复核的政策" not in _titles(client, student_headers, "&match_ready=1")

    resp = client.patch(f"/api/v1/rules/{rule_id}", headers=admin_headers,
                        json={"review_status": "approved"})
    assert resp.get_json()["code"] == 0

    assert "（R21）新复核的政策" in _titles(client, student_headers, "&match_ready=1")


def test_revoked_approval_removes_the_document(client, admin_headers, student_headers, app):
    """撤销复核（approved → needs_review）后，该政策必须从列表消失。"""
    doc = _doc(app, "（R21）将被撤销的政策")
    rule_id = _rule(app, doc)
    assert "（R21）将被撤销的政策" in _titles(client, student_headers, "&match_ready=1")

    resp = client.patch(f"/api/v1/rules/{rule_id}", headers=admin_headers,
                        json={"review_status": "needs_review"})
    assert resp.get_json()["code"] == 0

    assert "（R21）将被撤销的政策" not in _titles(client, student_headers, "&match_ready=1")


def test_document_with_several_rules_needs_only_one_approved(client, student_headers, app):
    """多条规则里只要有一条已复核资格条件就够——不能要求"全部通过"。"""
    doc = _doc(app, "（R21）部分复核")
    _rule(app, doc, review="needs_review")
    _rule(app, doc)
    _rule(app, doc, rule_type="deadline")

    assert "（R21）部分复核" in _titles(client, student_headers, "&match_ready=1")


# ============================================================ 3. 不影响知识库
def test_knowledge_base_still_lists_everything(client, student_headers, app):
    """不带 `match_ready` 时一份不少——这是"只清页面选项、不动知识库"的断言。"""
    ready = _doc(app, "（R21）知识库甲")
    _rule(app, ready)
    _doc(app, "（R21）知识库乙")

    all_titles = _titles(client, student_headers)
    assert "（R21）知识库甲" in all_titles
    assert "（R21）知识库乙" in all_titles
    assert len(all_titles) > len(_titles(client, student_headers, "&match_ready=1"))


def test_match_ready_zero_or_false_means_no_filter(client, student_headers, app):
    """显式传 0/false 等价于不过滤——前端只传 1，但参数语义要自洽。"""
    _doc(app, "（R21）零规则政策")
    full = _titles(client, student_headers)
    assert _titles(client, student_headers, "&match_ready=0") == full
    assert _titles(client, student_headers, "&match_ready=false") == full


# ============================================================ 4. 参数与分页
def test_match_ready_rejects_unknown_value(client, student_headers):
    resp = client.get("/api/v1/documents?match_ready=yes", headers=student_headers)
    body = resp.get_json()
    assert body["code"] == 1001
    assert "match_ready" in body["message"]


def test_match_ready_total_matches_filtered_set(client, student_headers, app):
    """`total` 必须按过滤后的集合算：前端分页要靠它，对不上会出现"第 2 页是空的"。"""
    for index in range(3):
        _rule(app, _doc(app, f"（R21）可匹配-{index}"))
    _doc(app, "（R21）不可匹配-0")
    _doc(app, "（R21）不可匹配-1")

    resp = client.get("/api/v1/documents?page=1&page_size=2&match_ready=1",
                      headers=student_headers)
    data = resp.get_json()["data"]
    assert data["total"] == 3
    assert len(data["items"]) == 2
    second = client.get("/api/v1/documents?page=2&page_size=2&match_ready=1",
                        headers=student_headers).get_json()["data"]
    assert len(second["items"]) == 1
    # 两页合起来正好是那 3 份，不重不漏
    ids = {i["id"] for i in data["items"]} | {i["id"] for i in second["items"]}
    assert len(ids) == 3


# ============================================================ 5. 匹配本身仍可用
def test_match_still_works_and_isolated(client, student_headers, app):
    """筛掉 15 份之后，剩下 3 份的匹配不受影响，也不会串用彼此的条件。"""
    a = _doc(app, "（R21）匹配甲")
    _rule(app, a, code="R-A")
    b = _doc(app, "（R21）匹配乙")
    with app.app_context():
        chunk_id = db.session.execute(
            db.select(DocumentChunk.id).where(DocumentChunk.document_id == b)
        ).scalars().first()
        db.session.add(PolicyRule(
            id=new_ulid(), document_id=b, chunk_id=chunk_id, rule_code="R-B",
            rule_type="eligibility", description="绩点不低于 4.0", field_key="gpa",
            operator="gte", expected_value="4.0",
            original_quote="平均学分绩点不低于 3.5。", quote_verified=1,
            extraction_status="verified", review_status="approved",
        ))
        db.session.commit()

    listed = _titles(client, student_headers, "&match_ready=1")
    assert set(listed) == {"（R21）匹配甲", "（R21）匹配乙"}

    body = client.post("/api/v1/matches", headers=student_headers,
                       json={"document_id": a, "profile": {"gpa": 3.5}}).get_json()
    assert body["code"] == 0
    assert {i["rule_code"] for i in body["data"]["items"]} == {"R-A"}
    assert body["data"]["items"][0]["status"] == "met"

    body_b = client.post("/api/v1/matches", headers=student_headers,
                         json={"document_id": b, "profile": {"gpa": 3.5}}).get_json()
    assert {i["rule_code"] for i in body_b["data"]["items"]} == {"R-B"}
    assert body_b["data"]["items"][0]["status"] == "not_met"
