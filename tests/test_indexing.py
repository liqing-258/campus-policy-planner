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
    """**记录一条被实测推翻过一次的结论，以及它为什么变了。**

    M3 当时（只有 jieba 分词、无 bigram）实测：
        转专业学分认定流程（无关）  top1 = 1.7997   ← 比下面那条相关查询还高
        申请要交哪些材料（相关）    top1 = 1.3687
    由此得出"裸 BM25 分数不能判断相关性，必须用 coverage"，M4 的拒答也据此实现。

    M4 给 tokenize() 加上字符 bigram 之后，同一对样本的排序**反过来了**：
        相关 2.255 > 无关 1.9205
    原因：无关查询原先靠"专业""学分"各撞上一个词而拿高分；现在片段侧多了
    「专业/业排」「学分/分绩」等 bigram，相关查询的"申请""材料"也能稳定命中，
    词项覆盖更充分的一方自然胜出。

    所以这条用例改成**断言当前事实**，而不是固守旧结论：
    - 覆盖率必须能区分（始终成立，拒答依赖它）
    - 分数在此样本上也恰好能区分，但**两个样本不足以据此定阈值**，
      真正的定标在 eval/qa_set.jsonl 的 24 条上做（tools/run_eval.py --sweep）
    """
    index = Bm25Index().build(_sample_rows())
    relevant = index.search("申请要交哪些材料", top_k=1)[0]
    irrelevant = index.search("转专业学分认定流程", top_k=1)[0]

    assert relevant.coverage > irrelevant.coverage, "覆盖率必须能区分，拒答依赖它"
    assert relevant.score > irrelevant.score, (
        "bigram 之后相关查询分数应更高；若此断言失败，说明分词或权重又动了，需重跑评测集重新定标"
    )


def test_bigram_recovers_word_split_inconsistency():
    """**M4 修掉的真缺陷**：同一个词在查询与片段里切法不同，导致零命中。

    实测：片段「上一学年成绩单」被 jieba 切成 '学年成绩'，
    查询「那成绩单要原件吗」切成 '成绩单' → 字面交集为空 → 检索判定无命中，
    用户问"成绩单要什么格式"会得到"未找到足够依据"，而原文里白纸黑字写着。

    加自定义词典也治不好（实测 jieba.add_word("成绩单") 后该句仍切出"学年成绩"），
    因此改用字符 bigram 兜底：片段产出「学年/年成/成绩」，查询产出「成绩/绩单」，
    交集"成绩"即可召回。
    """
    from app.indexing import tokenize

    query_terms = set(tokenize("那成绩单要原件吗？"))
    chunk_terms = set(tokenize("上一学年成绩单"))
    assert query_terms & chunk_terms, "bigram 应让切法不一致的词仍有交集"

    # 关掉 bigram 可以复现旧行为，用于对比实验
    assert not (set(tokenize("那成绩单要原件吗？", bigrams=False))
                & set(tokenize("上一学年成绩单", bigrams=False)))


def test_two_char_word_is_not_double_counted():
    """两字词的 bigram 就是它自己，必须排除，否则 tf 凭空翻倍、覆盖率分母被撑大。"""
    from app.indexing import tokenize

    assert tokenize("申请") == ["申请"]
    assert tokenize("成绩单") == ["成绩单", "成绩", "绩单"]


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


# ---------------------------------------------------------------- 重启后的懒加载
def test_index_is_lazily_loaded_after_process_restart(client, admin_headers, app):
    """模拟"进程重启"：库里还有片段，但内存索引空了。

    2026-11-08 联调真踩到：`/health` 报 document_count=4、chunk_count=10，
    `/qa/ask` 却答"知识库为空：尚未上传任何已解析的政策文件"——
    因为索引不落盘，重启后没人重建它，而报错信息把人指向"去重新上传文件"这个错误方向。
    """
    from pathlib import Path

    from app.indexing import EXTENSION_KEY, Bm25Index, ensure_index_loaded, get_index

    pdf = Path(__file__).resolve().parent.parent / "demo" / "01-本科生奖学金评审办法（演示样例）.pdf"
    with open(pdf, "rb") as handle:
        client.post("/api/v1/documents", headers=admin_headers,
                    data={"file": (handle, pdf.name), "title": "（演示样例）懒加载"},
                    content_type="multipart/form-data")

    with app.app_context():
        assert get_index().size > 0

        # 重启 = 换一个全新的空索引，且清掉"已尝试过懒加载"的进程内标记
        app.extensions[EXTENSION_KEY] = Bm25Index()
        app.extensions.pop("bm25_index_loaded", None)

        assert ensure_index_loaded() is True
        assert get_index().size > 0
        # 第二次调用不该再重建（已经在库为空时也不会反复扫表）
        assert ensure_index_loaded() is False


def test_ensure_loaded_does_not_rebuild_when_library_is_empty(app):
    """空库不应反复扫表，更不该把"确实没有文件"变成每次查询都建一次索引。"""
    from app.indexing import EXTENSION_KEY, Bm25Index, ensure_index_loaded

    with app.app_context():
        app.extensions[EXTENSION_KEY] = Bm25Index()
        app.extensions.pop("bm25_index_loaded", None)
        assert ensure_index_loaded() is False
        assert ensure_index_loaded() is False
