"""书名号锚定（R4）的测试。

**这份测试守的是一条"答错版本"的缺陷，不是一个新功能**

真实复现（演示库、降级模式、无 API Key）：

    问：国家励志奖学金的申请条件有哪些？            → 正常，9 条候选
    点「继续追问」（前端拼的是《本科生奖学金评审办法（2026年版）》里还有哪些…）
                                                    → 候选只剩 2 条，第 1 名是 **2025 版**

根因不是检索质量，是**书名号里的年份被当成了主题词**：2026 版的正文只写"本办法"，
字面上根本没有"2026"；反倒是 2025 版第一章印着"（2025年版）"。于是被用户点名的那份
文件被自己的年份压到 0.308（阈值 0.35），整份出闸门。

所以下面钉四件事：

1. **点名就只答那份**：书名号对得上库内文件时，候选全部来自该文件；
2. **版本要点准**：《…（2026年版）》不能把 2025 版排上来；
3. **没点名就一点不变**：普通问句的锚定字段必须为空，行为与 R3 逐位一致
   （这是回滚路径——出问题先关功能，而不是改代码）；
4. **对不上的书名号不许硬凑**：库里没有那份文件时退回普通检索，不猜。
"""

from __future__ import annotations

from datetime import date

import pytest

from app import anchors
from app.contract import new_ulid
from app.models import Document, DocumentChunk, User, db

# 两版办法 + 一份不相干的文件。
# 关键构造：**2026 版的正文里不出现"2026"**（真实 PDF 就是这样：年份只印在文件名上），
# 而 2025 版第一章正文里印着"（2025年版）"——正是它让 2025 版在原实现里排到第一。
DOCS: list[tuple[str, str | None, list[tuple[str, str]]]] = [
    ("本科生奖学金评审办法（2026年版）", "2026", [
        ("第一章 总则 为规范本校本科生奖学金评审工作，根据国家有关规定并结合本校实际，"
         "制定本办法。本办法适用于具有本校正式学籍的全日制在校本科学生。", "第一章 总则"),
        ("第二章 申请条件 第四条 申请国家励志奖学金，应当同时具备下列条件："
         "学年平均学分绩点不低于 3.0，且专业排名位于前 30%。", "第二章 申请条件"),
        ("第三章 评审程序与时间安排 第六条 奖学金评审按下列程序进行：学生本人提出申请，"
         "学院初审，公示 3 个工作日。", "第三章 评审程序与时间安排"),
    ]),
    ("本科生奖学金评审办法（2025年版）", "2025", [
        ("示例大学本科生奖学金评审办法（2025年版） 第一章 总则 为规范本校本科生奖学金"
         "评审工作，根据国家有关规定并结合本校实际，制定本办法。", "第一章 总则"),
        ("第二章 申请条件 第四条 申请国家励志奖学金，应当同时具备下列条件："
         "学年平均学分绩点不低于 2.8，且专业排名位于前 30%。", "第二章 申请条件"),
        ("第三章 评审程序与时间安排 第六条 奖学金评审按下列程序进行：学生本人提出申请，"
         "学院初审，公示 5 个工作日。", "第三章 评审程序与时间安排"),
    ]),
    ("图书馆借阅与自习空间管理办法", None, [
        ("第一章 开放时间 图书馆周一至周日开放，借阅区 8:00 至 22:00。", "第一章 开放时间"),
        ("第二章 借阅规则 本科生可借阅图书 10 册，逾期未还的按每册每天 0.1 元收取逾期费。",
         "第二章 借阅规则"),
    ]),
]

V2026 = "本科生奖学金评审办法（2026年版）"
V2025 = "本科生奖学金评审办法（2025年版）"
LIB = "图书馆借阅与自习空间管理办法"


@pytest.fixture()
def kb(app):
    """把三份文件写进库并重建索引，返回 {标题: document_id}。"""
    with app.app_context():
        uploader_id = db.session.execute(db.select(User.id)).scalars().first()
        ids: dict[str, str] = {}
        for title, year, chunks in DOCS:
            document = Document(
                id=new_ulid(), title=title, file_name=f"{title}.txt",
                stored_name=f"{new_ulid()}.txt", file_type="txt", file_size=1024,
                parse_status="indexed", chunk_count=len(chunks), uploader_id=uploader_id,
                published_at=date(int(year), 9, 1) if year else None,
                version_label=year, effective_status="effective",
            )
            db.session.add(document)
            for position, (content, section) in enumerate(chunks):
                db.session.add(DocumentChunk(
                    id=new_ulid(), document_id=document.id, chunk_index=position,
                    content=content, char_start=position * 100, char_end=(position + 1) * 100,
                    section_title=section,
                ))
            ids[title] = document.id
        db.session.commit()

        from app.indexing import rebuild_index
        rebuild_index()
    return ids


def _retrieve(app, query: str, **kwargs):
    """在应用上下文里跑一次检索（`retrieve` 需要 settings 与已建好的索引）。"""
    from app.retrieval import retrieve
    with app.app_context():
        return retrieve(query, **kwargs)


# ---------------------------------------------------------------- 纯函数：匹配规则


def test_quote_is_extracted_with_length_limits():
    """书名号内容必须能被取出来——这条看着废话，第一版正则写成 `{60}`（恰好 60 字）
    时它是空的，而"锚定不生效"的表现是**静默退回普通检索**，不会报错。"""
    assert anchors.quotes_in("《图书馆借阅与自习空间管理办法》里有哪些规定") == [
        "图书馆借阅与自习空间管理办法"
    ]
    assert anchors.quotes_in("没有书名号的问句") == []


def test_exact_title_pins_one_version():
    result = anchors.resolve(
        "《本科生奖学金评审办法（2026年版）》里还有哪些规定",
        [(V2026, V2026), (V2025, V2025)],
    )
    assert result is not None
    assert result.pinned is True
    assert result.titles == (V2026,)


def test_half_width_brackets_still_match():
    """库里存的是全角「（2026年版）」，用户手打半角也要能对上（NFKC 归一化）。"""
    result = anchors.resolve(
        "《本科生奖学金评审办法(2026年版)》有哪些规定",
        [(V2026, V2026), (V2025, V2025)],
    )
    assert result is not None and result.titles == (V2026,)


def test_title_without_version_matches_both_and_is_not_pinned():
    result = anchors.resolve(
        "《本科生奖学金评审办法》里有哪些规定",
        [(V2026, V2026), (V2025, V2025)],
    )
    assert result is not None
    assert result.pinned is False
    assert set(result.titles) == {V2026, V2025}


def test_unknown_title_is_not_forced():
    """库里没有这份文件就**不许猜**：退回普通检索，由闸门和模型去判。"""
    assert anchors.resolve("《不存在的管理办法》里有哪些规定", [(V2026, V2026)]) is None


def test_short_quote_does_not_match_everything():
    """《办法》这种两字书名号会命中所有文件，那不是锚定，是把范围缩成整个库。"""
    assert anchors.resolve("《办法》里有哪些规定", [(V2026, V2026), (LIB, LIB)]) is None


def test_allowed_scope_wins_over_quote():
    """调用方已经限定了范围时，书名号不能突破它（越权/越界的口子不能开在这里）。"""
    assert anchors.resolve("《图书馆借阅与自习空间管理办法》有哪些规定",
                           [(V2026, V2026), (LIB, LIB)], allowed=[V2026]) is None


def test_residual_keeps_core_name_and_drops_year():
    """年份去掉、核心名留下：年份是死词（正文里没有），核心名是主题词。

    不刻意补空格：书名号后面直接跟着"里"时拼成"办法里"，jieba 照样切成"办法/里"。
    断言的实质是**年份与括号消失、核心名还在**，不是空格位置。
    """
    residual = anchors.residual_text("《本科生奖学金评审办法（2026年版）》里还有哪些规定")
    assert "2026" not in residual and "年版" not in residual
    assert residual.startswith("本科生奖学金评审办法")
    assert residual.endswith("里还有哪些规定")


def test_open_query_only_when_no_topic_word():
    assert anchors.is_open_query(["还有", "问题", "规定"]) is True
    assert anchors.is_open_query(["办理", "流程"]) is False
    assert anchors.is_open_query([]) is True


# ---------------------------------------------------------------- 端到端：检索


def test_named_version_wins_over_the_older_one(app, kb):
    """缺陷本体：点名 2026 版，候选里不能出现 2025 版，且不能拒答。

    修复前的实测是反的——第 1 名 2025 版第一章（0.769），2026 版只剩第一章（0.692），
    第二/三章全部 0.308 被闸门挡掉。
    """
    out = _retrieve(app, "《本科生奖学金评审办法（2026年版）》里还有哪些与这个问题相关的规定？")
    assert out.refused is False
    assert out.anchor is not None and out.anchor["pinned"] is True
    assert {c.document_title for c in out.candidates} == {V2026}, (
        "点名了 2026 版，就不能把 2025 版摆进证据里"
    )
    assert all(c.via_anchor for c in out.candidates)
    # 三章都在：修复前只有第一章能过闸
    assert len(out.candidates) == len(DOCS[0][2])
    assert out.anchor_gate == "document_scoped"


def test_open_query_lists_the_named_document_in_order(app, kb):
    """`《X》里还有哪些规定`：按章节顺序摆，而不是按"谁撞上了规定两个字"。"""
    out = _retrieve(app, "《本科生奖学金评审办法（2026年版）》有哪些规定")
    order = [c.chunk_index for c in out.candidates]
    assert order == sorted(order), "开放式问法应按文件内顺序"
    assert [c.section_title for c in out.candidates][0] == "第一章 总则"


def test_versionless_name_interleaves_both_versions(app, kb):
    """不带年份时两版都要在，且按章节交错——用户要看的是"两版差在哪"。

    窗口只有 6 条（默认 top_k），先排完一版就会把另一版挤掉。
    """
    out = _retrieve(app, "《本科生奖学金评审办法》里还有哪些规定")
    assert out.anchor is not None and out.anchor["pinned"] is False
    assert {c.document_title for c in out.candidates} == {V2026, V2025}
    kinds = [c.chunk_index for c in out.candidates]
    assert kinds[0] == kinds[1] == 0, "第一章两版应相邻，便于逐条对照"


def test_topic_inside_named_document_stays_scoped(app, kb):
    """点名文件 + 另一个主题：仍然只答点名文件，不跑去答别的文件。

    这是**刻意**的取舍：能不能答由闸二（模型自评证据充分性）判，不由覆盖率判。
    代价写在设计 §7.2.5：模型会说"这份文件没写"，而不是拿另一份文件的条款顶上。
    """
    out = _retrieve(app, "《图书馆借阅与自习空间管理办法》里关于转专业的规定？")
    assert out.refused is False
    assert {c.document_title for c in out.candidates} == {LIB}


def test_plain_question_is_not_anchored_and_stays_unchanged(app, kb):
    """没写书名号的问句：锚定字段必须为空——这是回滚路径。"""
    out = _retrieve(app, "国家励志奖学金的申请条件有哪些？")
    assert out.anchor is None
    assert out.anchor_gate is None
    assert out.refused is False
    titles = {c.document_title for c in out.candidates}
    assert titles == {V2026, V2025}, "普通问句仍应在全部文件里检索（含两版）"


def test_unknown_document_falls_back_to_normal_retrieval(app, kb):
    """对不上的书名号不锚定，也不额外造错误：行为与普通检索一致。"""
    out = _retrieve(app, "《不存在的管理办法》里有哪些规定？")
    assert out.anchor is None
    assert out.anchor_gate is None


def test_anchor_survives_the_rewritten_query(app, kb):
    """改写把书名号改写掉时，锚定仍要按**原话**生效。

    真实链路里改写结果可能被重写成"本科生奖学金评审办法的申请条件"，
    若只看改写结果，点名信息就丢了——而丢的方式是静默的。
    """
    out = _retrieve(
        app,
        "《本科生奖学金评审办法（2026年版）》里还有哪些规定",
        standalone_query="本科生奖学金评审办法 还有哪些规定",
        expanded_terms=["评审办法", "申请条件"],
    )
    assert out.anchor is not None and out.anchor["pinned"] is True
    assert {c.document_title for c in out.candidates} == {V2026}
