"""T-12 政策版本冲突检测测试。

设计写的是"两版政策冲突 → 并列展示 + 日期差异提示"。这里拆成两层验：

- **纯函数层**（normalize_title / year_of / detect）：规则本身对不对；
- **链路层**：一份 2025 版与一份 2026 版同时被召回时，问答响应里真的带上了
  并列信息与日期差，而不是只有单测里成立。

为什么要这么小心：版本提示一旦误报，用户就开始忽略它，这比不提示更糟。
所以"不该报的"用例条数刻意多于"该报的"。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import versions
from app.contract import new_ulid
from app.models import Document, DocumentChunk, db


# ============================================================ 标题规范化
@pytest.mark.parametrize("title,expected", [
    ("（演示样例）2026 年本科生奖学金评审办法（修订版）.pdf", "本科生奖学金评审办法"),
    ("本科生奖学金评审办法（2025年修订）", "本科生奖学金评审办法"),
    ("关于本科生奖学金评审办法的补充通知 v2", "关于本科生奖学金评审办法的补充通知"),
])
def test_normalize_title_strips_year_version_and_parens(title, expected):
    assert versions.normalize_title(title) == expected


def test_normalize_keeps_different_topics_apart():
    """压得狠是为了容纳改版，不是为了把不同主题压成同一个。"""
    a = versions.normalize_title("本科生奖学金评审办法")
    b = versions.normalize_title("本科生勤工助学管理办法")
    assert a != b


@pytest.mark.parametrize("title,academic_year,expected", [
    ("2026 年奖学金评审办法", None, 2026),
    ("奖学金评审办法", "2025-2026", 2025),
    ("奖学金评审办法", None, None),
])
def test_year_of_prefers_title_then_academic_year(title, academic_year, expected):
    assert versions.year_of(title, academic_year) == expected


# ============================================================ detect 纯逻辑
def _doc(app, title, *, academic_year=None, published_at=None,
         effective_status="unknown", chunks=(("奖学金 申请 条件 材料",),)):
    with app.app_context():
        uploader = db.session.execute(
            db.select(db.text("id")).select_from(db.text("users"))).first()[0]
        document = Document(
            id=new_ulid(), title=title, file_name="p.pdf",
            stored_name=f"{new_ulid()}.pdf", file_type="pdf", file_size=100,
            parse_status="indexed", chunk_count=1, uploader_id=uploader,
            academic_year=academic_year, published_at=published_at,
            effective_status=effective_status,
        )
        db.session.add(document)
        for index, (content,) in enumerate(chunks):
            db.session.add(DocumentChunk(
                id=new_ulid(), document_id=document.id, chunk_index=index,
                content=content, char_start=0, char_end=len(content),
                page_number=1, section_title="第一章",
            ))
        db.session.commit()
        return document.id


def _candidates(*document_ids):
    return [
        SimpleNamespace(document_id=doc_id, index=index)
        for index, doc_id in enumerate(document_ids, 1)
    ]


def test_detect_reports_two_versions_of_the_same_policy(app):
    from datetime import date

    old = _doc(app, "2025 年本科生奖学金评审办法", academic_year="2025-2026",
               published_at=date(2025, 9, 1), effective_status="superseded")
    new = _doc(app, "2026 年本科生奖学金评审办法", academic_year="2026-2027",
               published_at=date(2026, 9, 1), effective_status="active")

    with app.app_context():
        conflicts = versions.detect(_candidates(old, new))

    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict["newest_document_id"] == new, "较新的那份必须排在前面"
    assert conflict["day_gap"] == 365
    assert [d["year"] for d in conflict["documents"]] == [2026, 2025]
    # 提示里必须点出年份跨度、日期差与"哪份现行有效"，否则用户仍然不知道信谁
    assert "2025" in conflict["hint"] and "2026" in conflict["hint"]
    assert "365" in conflict["hint"]
    assert "现行有效" in conflict["hint"]


def test_detect_is_silent_for_different_topics(app):
    a = _doc(app, "2026 年本科生奖学金评审办法", academic_year="2026-2027")
    b = _doc(app, "2025 年本科生勤工助学管理办法", academic_year="2025-2026")
    with app.app_context():
        assert versions.detect(_candidates(a, b)) == []


def test_detect_is_silent_when_only_one_document(app):
    only = _doc(app, "2026 年本科生奖学金评审办法")
    with app.app_context():
        assert versions.detect(_candidates(only)) == []


def test_detect_skips_very_short_titles(app):
    """两份都叫《通知》的文件不该被当成同一政策的两个版本。"""
    a = _doc(app, "2025 年通知", academic_year="2025-2026")
    b = _doc(app, "2026 年通知", academic_year="2026-2027")
    with app.app_context():
        assert versions.detect(_candidates(a, b)) == []


def test_detect_is_silent_when_year_and_date_agree(app):
    """同年同日期更像重复上传，交给去重逻辑；报成"版本冲突"会让人去核对不存在的问题。"""
    from datetime import date

    a = _doc(app, "2026 年本科生奖学金评审办法", academic_year="2026-2027",
             published_at=date(2026, 9, 1))
    b = _doc(app, "本科生奖学金评审办法（2026 版）", academic_year="2026-2027",
             published_at=date(2026, 9, 1))
    with app.app_context():
        assert versions.detect(_candidates(a, b)) == []


def test_detect_flags_conflicting_active_flags(app):
    """两份都标 active 时不能替用户选一个，必须说"标注本身有冲突"。"""
    from datetime import date

    a = _doc(app, "2026 年本科生奖学金评审办法", academic_year="2026-2027",
             published_at=date(2026, 9, 1), effective_status="active")
    b = _doc(app, "2025 年本科生奖学金评审办法", academic_year="2025-2026",
             published_at=date(2025, 9, 1), effective_status="active")
    with app.app_context():
        conflicts = versions.detect(_candidates(a, b))
    assert "标注本身存在冲突" in conflicts[0]["hint"]


# ============================================================ 链路
def test_ask_surfaces_both_versions(app, client, student_headers, llm_on, mock_llm):
    """两份版本都被召回时，问答响应里要能并列看到它们。"""
    from datetime import date

    old = _doc(app, "2025 年本科生奖学金评审办法", academic_year="2025-2026",
               published_at=date(2025, 9, 1), effective_status="superseded",
               chunks=(("申请奖学金须提交申请表、成绩单与认定告知书。",),))
    new = _doc(app, "2026 年本科生奖学金评审办法", academic_year="2026-2027",
               published_at=date(2026, 9, 1), effective_status="active",
               chunks=(("申请奖学金须提交申请表、成绩单与公示材料。",),))
    with app.app_context():
        from app.indexing import rebuild_index

        rebuild_index()

    llm_on()
    mock_llm('{"answer":"需要申请表与成绩单 [1][2]。","cited":[1,2],"sufficient":true,'
             '"confidence":"high"}')

    body = client.post("/api/v1/qa/ask", headers=student_headers,
                       json={"question": "申请奖学金需要提交哪些材料？"}).get_json()
    assert body["code"] == 0, body
    conflicts = body["data"]["conflicts"]
    assert len(conflicts) == 1, conflicts
    assert conflicts[0]["newest_document_id"] == new
    assert {d["document_id"] for d in conflicts[0]["documents"]} == {old, new}
    # 引用编号也要标出来，前端才能把"答案引用了哪一版"高亮
    assert conflicts[0]["cited_indexes"]
