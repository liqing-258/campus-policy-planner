"""追问建议（R5）的测试。

**这份测试守的是一条"点下去答不出来"的体验缺陷，不是文案**

改之前，前端用意图标签拼追问：`{label}需要准备哪些材料？`、
`{label}的申请截止时间和办理流程是什么？`。标签是分类器的输出，不是语料承诺：

    问「课程考核怎么算平时分」 → label「学分与成绩」 → 追问「学分与成绩的申请截止时间…」
    问「校园卡补办要多少钱」   → label「其他事项」   → 追问「其他事项需要准备哪些材料？」

这两句在 18 份文件里都找不到答案。实测 18×3=54 条追问，9 条必拒答；
把语料补全后仍有 2~3 条在批次间摇摆（模型对"没内容的问题"判定不稳定）。

改法是**从证据派生**：文件、章节、以及"这轮没引用到的章节"都是确定的。
下面钉五件事：

1. 每一条都是书名号问句（R4 的锚定保证它有检索范围）；
2. 「不符合…会怎么处理」只给义务性规范（办法/规定），不给指南与处分类文件；
3. 章节题只挑**这轮没引用过**的章节；
4. 最多 3 条，不重复；
5. 没有候选时返回空（不要在没答出来时编建议）。
"""

from __future__ import annotations

import pytest

from app.contract import new_ulid
from app.models import Document, DocumentChunk, User, db
from app.qa import NEXT_QUESTION_LIMIT, build_next_questions
from app.retrieval import Candidate

SECTIONS_BAN = ["第一章 总则", "第二章 申请条件", "第三章 评审程序与时间安排", "第四章 违规处理"]
SECTIONS_GUIDE = ["第一章 门诊与急诊", "第二章 医保与报销", "第八章 不按规定就医的处理"]
SECTIONS_DISCIPLINE = ["第一章 处分种类与期限", "第二章 违纪情形", "第三章 处分程序"]


@pytest.fixture()
def kb(app):
    """三份文件：办法（义务性）、指南、处分办法。返回 {标题: [section, ...]}。"""
    with app.app_context():
        uploader = db.session.execute(db.select(User.id)).scalars().first()
        planted: dict[str, list[str]] = {}
        plan = [
            ("示例大学本科生奖学金评审办法", SECTIONS_BAN),
            ("示例大学校医院就诊指南", SECTIONS_GUIDE),
            ("示例大学学生违纪处分与申诉管理办法", SECTIONS_DISCIPLINE),
        ]
        for title, sections in plan:
            document = Document(
                id=new_ulid(), title=title, file_name=f"{title}.pdf",
                stored_name=f"{new_ulid()}.pdf", file_type="pdf", file_size=1024,
                parse_status="indexed", chunk_count=len(sections), uploader_id=uploader,
            )
            db.session.add(document)
            for position, section in enumerate(sections):
                body = f"{section} 本条规定示例内容，用于验证追问建议的派生逻辑。" * 3
                db.session.add(DocumentChunk(
                    id=new_ulid(), document_id=document.id, chunk_index=position,
                    content=body, char_start=position * 100, char_end=(position + 1) * 100,
                    section_title=section,
                ))
            planted[title] = sections
        db.session.commit()

        from app.indexing import rebuild_index
        rebuild_index()
    return planted


def _candidates(document_title: str, sections: list[str], *, cited: int = 1) -> list[Candidate]:
    """造前 `cited` 个章节的候选，模拟"这轮引用过这几段"。"""
    from app.indexing import get_index

    index = get_index()
    doc_id = next(
        c.document_id for c in index._chunks if c.document_title == document_title
    )
    out: list[Candidate] = []
    for position, section in enumerate(sections[:cited], 1):
        chunk = next(c for c in index.chunks_of(doc_id) if c.section_title == section)
        out.append(Candidate(
            index=position, chunk_id=chunk.chunk_id, document_id=doc_id,
            document_title=document_title, content=chunk.content,
            page_number=None, section_title=section,
            score=1.0, coverage=1.0, chunk_index=chunk.chunk_index,
        ))
    return out


def test_every_suggestion_names_a_document(app, kb):
    """每条建议都含《文件名》——这是 R4 锚定生效的前提，也是"点下去有范围"的保证。

    注意"不符合《X》规定的情况…"是以"不符合"开头的：断言的是**书名号出现**，
    不是"以书名号开头"。锚定只要求问句里出现能对上的《…》。
    """
    title = "示例大学本科生奖学金评审办法"
    with app.app_context():
        questions = build_next_questions("奖助学金", _candidates(title, SECTIONS_BAN))
    assert questions
    assert all(f"《{title}》" in q for q in questions)


def test_obligational_document_gets_the_penalty_question(app, kb):
    """义务性规范（办法/规定）才有"不符合规定会怎么处理"——这条最像人话，也最常被点。"""
    title = "示例大学本科生奖学金评审办法"
    with app.app_context():
        questions = build_next_questions("奖助学金", _candidates(title, SECTIONS_BAN))
    assert "不符合《示例大学本科生奖学金评审办法》规定的情况会怎么处理？" in questions


def test_guide_document_uses_the_section_title_instead(app, kb):
    """指南类文件没有"遵守/违反"关系，用章节标题提问；标签式问法会让模型判证据不足。"""
    title = "示例大学校医院就诊指南"
    with app.app_context():
        questions = build_next_questions("医疗服务", _candidates(title, SECTIONS_GUIDE))
    assert not any(q.startswith("不符合") for q in questions)
    assert any("不按规定就医的处理" in q for q in questions)


def test_discipline_document_does_not_ask_about_violating_itself(app, kb):
    """处分类文件问"不符合它自己会怎么处理"是套娃，实测模型会拒答。"""
    title = "示例大学学生违纪处分与申诉管理办法"
    with app.app_context():
        questions = build_next_questions("其他事项", _candidates(title, SECTIONS_DISCIPLINE))
    assert not any(q.startswith("不符合") for q in questions)


def test_section_question_skips_what_was_already_cited(app, kb):
    """章节题挑"这轮没引用过"的：刚看完第二章，接着问第三章才有意义。"""
    title = "示例大学校医院就诊指南"
    with app.app_context():
        questions = build_next_questions("医疗服务", _candidates(title, SECTIONS_GUIDE, cited=2))
    section_questions = [q for q in questions if "」是怎么规定的" in q]
    assert section_questions
    asked = section_questions[0]
    assert "门诊与急诊" not in asked and "医保与报销" not in asked


def test_limit_and_dedupe(app, kb):
    title = "示例大学本科生奖学金评审办法"
    with app.app_context():
        questions = build_next_questions("奖助学金", _candidates(title, SECTIONS_BAN))
    assert len(questions) <= NEXT_QUESTION_LIMIT
    assert len(set(questions)) == len(questions)


def test_no_candidates_means_no_suggestions(app, kb):
    """没答出来的时候不要编建议：这时界面上展示的是拒答卡。"""
    with app.app_context():
        assert build_next_questions("奖助学金", []) == []
