"""BM25 索引与检索的单元测试。

M3 的门禁写的是"上传、解析、切分、索引"，前三个都有接口输出可看，
**索引没有**——它只在内存里。所以这里用"能不能检索到正确片段"来证明它真的建成了，
而不是靠"接口返回 200"自我安慰。
"""

from __future__ import annotations

from app.indexing import Bm25Index, tokenize


def test_tokenize_drops_stopwords_and_single_cjk_chars():
    tokens = tokenize("申请国家励志奖学金的同学需要提供成绩单")
    assert "奖学金" in "".join(tokens) or "奖学" in tokens or "奖学金" in tokens
    assert "的" not in tokens
    assert "需" not in tokens or len("需") > 1


def test_tokenize_handles_english_case_insensitive():
    assert "gpa" in tokenize("GPA 不低于 3.0")


def _sample_rows():
    return [
        {
            "chunk_id": "c1", "document_id": "d1",
            "document_title": "（演示样例）奖学金评审办法",
            "content": "第四条 申请国家励志奖学金，应当具备下列条件：学年平均学分绩点不低于 3.0，专业排名前 30%。",
            "page_number": 2, "section_title": "第二章 申请条件",
        },
        {
            "chunk_id": "c2", "document_id": "d1",
            "document_title": "（演示样例）奖学金评审办法",
            "content": "第九条 申请国家励志奖学金应当提交下列材料：申请表、成绩单、家庭经济困难认定表、个人申请书。",
            "page_number": 4, "section_title": "第四章 申请材料与附则",
        },
        {
            "chunk_id": "c3", "document_id": "d2",
            "document_title": "（演示样例）学生事务办理指南",
            "content": "在读证明办理时限为 2 个工作日，成绩单打印即时生成。",
            "page_number": None, "section_title": "四、时限与费用",
        },
    ]


def test_search_returns_relevant_chunk_first():
    index = Bm25Index().build(_sample_rows())
    hits = index.search("国家励志奖学金 申请条件", top_k=3)
    assert hits, "有明确关键词的问题必须能召回"
    assert hits[0].chunk_id == "c1"
    assert hits[0].score > 0


def test_search_matches_material_question_to_material_chunk():
    index = Bm25Index().build(_sample_rows())
    hits = index.search("申请要交哪些材料", top_k=3)
    assert hits
    assert hits[0].chunk_id == "c2"


def test_search_carries_citation_metadata():
    index = Bm25Index().build(_sample_rows())
    hit = index.search("成绩单 材料", top_k=1)[0]
    assert hit.page_number is not None or hit.section_title
    assert hit.document_title


def test_search_returns_empty_when_no_query_term_matches():
    """零交集的问题必须返回空——这是检索层唯一能自己搞定的"拒答"情形。"""
    index = Bm25Index().build(_sample_rows())
    assert index.search("宿舍水电费怎么交") == []


def test_raw_score_alone_cannot_tell_relevant_from_irrelevant():
    """**记录一个反直觉的实测结论：裸 BM25 分数不能用来判断相关性。**

    实测（本文件三条样例片段）：
        转专业学分认定流程（无关）  top1 = 1.7997  ← 比下面那条相关查询还高
        申请要交哪些材料（相关）    top1 = 1.3687
    所以"给 search() 加一个 0.35 的分数阈值就能拒答"是错的：那个阈值只会把零命中的情况筛掉。
    真正有区分度的是 coverage（命中查询词项占比）：相关 ≥ 0.67，无关 ≤ 0.5。
    """
    index = Bm25Index().build(_sample_rows())
    relevant = index.search("申请要交哪些材料", top_k=1)[0]
    irrelevant = index.search("转专业学分认定流程", top_k=1)[0]

    assert irrelevant.score > relevant.score, "断言分数不能区分——一旦这条不再成立，说明打分逻辑变了，需重新评估"
    assert relevant.coverage > irrelevant.coverage, "覆盖率必须能区分"


def test_coverage_filters_topically_unrelated_query():
    """覆盖率闸门：无关查询被筛掉，相关查询保留。"""
    index = Bm25Index().build(_sample_rows())

    assert index.search("转专业学分认定流程", min_coverage=0.6) == []
    assert index.search("保研推免条件", min_coverage=0.6) == []

    kept = index.search("申请要交哪些材料", top_k=3, min_coverage=0.6)
    assert kept and kept[0].chunk_id == "c2"
    assert kept[0].coverage >= 0.6


def test_hit_carries_matched_terms_for_explainability():
    """命中词项要能说出来——答辩时"为什么检索到这条"不能只会说分数。"""
    index = Bm25Index().build(_sample_rows())
    hit = index.search("家庭经济困难 认定", top_k=1)[0]
    assert hit.chunk_id == "c2"
    assert "认定" in hit.matched_terms
    assert 0.0 < hit.coverage <= 1.0


def test_search_respects_document_scope():
    index = Bm25Index().build(_sample_rows())
    hits = index.search("成绩单", top_k=10, document_ids=["d2"])
    assert hits
    assert all(hit.document_id == "d2" for hit in hits)


def test_empty_index_returns_nothing():
    assert Bm25Index().search("任意问题") == []


def test_stats_reflect_build():
    index = Bm25Index().build(_sample_rows())
    stats = index.stats()
    assert stats["chunks"] == 3
    assert stats["terms"] > 10
    assert stats["build_ms"] >= 0


def test_invalidate_clears_index():
    index = Bm25Index().build(_sample_rows())
    assert index.size == 3
    index.invalidate()
    assert index.size == 0
    assert index.search("奖学金") == []


# ---------------------------------------------------------------- 端到端
def test_uploaded_document_is_searchable(client, admin_headers, app):
    """走完整链路：上传 PDF → 解析 → 切分 → 建索引 → 能检索到。"""
    from pathlib import Path

    from app.indexing import get_index

    pdf = Path(__file__).resolve().parent.parent / "demo" / "01-本科生奖学金评审办法（演示样例）.pdf"
    with open(pdf, "rb") as handle:
        resp = client.post("/api/v1/documents", headers=admin_headers,
                           data={"file": (handle, pdf.name), "title": "（演示样例）索引端到端"},
                           content_type="multipart/form-data")
    doc_id = resp.get_json()["data"]["id"]

    with app.app_context():
        hits = get_index().search("家庭经济困难 认定", top_k=5)
    assert hits, "上传后应能检索到内容"
    assert all(hit.document_id == doc_id for hit in hits)
    assert any("家庭经济困难" in hit.content for hit in hits)
