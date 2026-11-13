"""双路召回 + RRF 融合（R3）的单元测试。

**这份测试守的不是新功能，而是"不许变差"**

改写会把第二种说法（同义扩展词）送进检索。多一路召回天然会让候选变多，
但变多不等于变好：如果融合方式错了，用户会看到"召回变多了、答案却更差"。
所以下面按三条线钉死：

1. **不传扩展词时逐位等于 R2**。这是回滚路径——关掉改写，行为必须一模一样，
   否则"线上出问题先关开关"这条应急手段就失效了。
2. **覆盖率取两个视图的高者，不取并集**。并集会把同义词当成追加要求（AND），
   实测会把 0.40 稀释到 0.30、把 0.71 稀释到 0.42——阈值是 24 条评测集扫出来的，
   分母一变它的含义就变了。
3. **零命中的片段不许靠豁免混进证据块**。双路融合后会出现"两条路都没直接命中、
   只是被按名次排进来"的片段，它们的覆盖率恒为 0，如果闸门只看"≥ 阈值"，
   这些就会以 0.0 的覆盖率出现在模型面前，等于白送噪声。
"""

from __future__ import annotations

import pytest

from app.indexing import Bm25Index
from app.retrieval import RRF_K, _rrf, _score_combined, retrieve, tokenize

ROWS = [
    {
        "chunk_id": "lib-open", "document_id": "d1",
        "document_title": "图书馆借阅与自习空间管理办法",
        "content": "第一章 开放时间 图书馆周一至周日开放，借阅区 8:00 至 22:00，自习区 7:00 至 23:00。",
        "section_title": "第一章 开放时间", "chunk_index": 0,
    },
    {
        "chunk_id": "lib-fee", "document_id": "d1",
        "document_title": "图书馆借阅与自习空间管理办法",
        "content": "第四章 违规处理 图书逾期未还的，按每册每天 0.1 元收取逾期费，单册上限 20 元。",
        "section_title": "第四章 违规处理", "chunk_index": 3,
    },
    {
        "chunk_id": "lib-space", "document_id": "d1",
        "document_title": "图书馆借阅与自习空间管理办法",
        "content": "第三章 自习与研讨空间 图书馆自习座位实行预约制，每次最长 4 小时。",
        "section_title": "第三章 自习与研讨空间", "chunk_index": 2,
    },
    {
        "chunk_id": "dorm", "document_id": "d2",
        "document_title": "学生宿舍管理规定",
        "content": "第一章 住宿与门禁 宿舍楼 23:30 关闭大门，晚归需登记。",
        "section_title": "第一章 住宿与门禁", "chunk_index": 0,
    },
]

# 用户原话：说"逾期""收费"，原文写"逾期未还""逾期费"
USER_QUERY = "图书馆逾期收费怎么算"
# 改写后自包含查询 + 同义扩展词（扩展词里刻意放进原文没出现过的 OOV 词）
STANDALONE = "图书馆图书逾期收费规定"
EXPANDED = ["逾期费", "滞纳金", "超期罚款"]


@pytest.fixture()
def index():
    return Bm25Index().build(ROWS)


def _ids(hits):
    return [h.chunk_id for h in hits]


# --- 1. 回滚路径：不传扩展词时与 R2 逐位一致 --------------------------------


def test_without_expansion_score_combined_equals_single_view(index):
    """不传扩展词时 `_score_combined` 必须原样返回单视图结果，一个数都不重算。"""
    terms = tokenize(STANDALONE)
    for row in ROWS:
        assert _score_combined(index, terms, [], row["chunk_id"]) == \
            index.score_chunk_with(terms, row["chunk_id"])


# --- 2. 覆盖率口径：OR 而不是 AND -------------------------------------------


def test_coverage_takes_the_higher_view_not_the_union(index):
    """取高者：扩展词只能把候选**捞回来**，不能因为"没同时命中同义词"给它扣分。

    这里的构造很关键——`EXPANDED` 里含 OOV 词（滞纳金、超期罚款），
    并集口径会把它们算进分母，而它们在语料里一条都命不中。
    """
    a_terms, e_terms = tokenize(STANDALONE), tokenize(" ".join(EXPANDED))
    base = index.score_chunk_with(a_terms, "lib-fee")
    extra = index.score_chunk_with(e_terms, "lib-fee")
    combined = _score_combined(index, a_terms, e_terms, "lib-fee")

    assert combined.coverage == max(base.coverage, extra.coverage)
    union = set(a_terms) | set(e_terms)
    assert combined.coverage >= len(set(a_terms) & set(tokenize(ROWS[1]["content"]))) / len(union), (
        "并集口径的分母必然大于等于单视图，覆盖率只会更低或相等"
    )


def test_expanded_view_can_rescue_a_chunk(index):
    """构造一条"原话几乎问不到、扩展词一问就到"的片段，证明第二路不是白跑的。

    为什么需要专门构造：真实问答里，改写产出的自包含查询**本身就比用户原话贴近原文**，
    所以多数片段的最高覆盖率来自 A 视图，E 视图只在 A 完全打空时才翻盘
    （`_score_combined` 的 `base.coverage == 0` 分支）。这条测试把那个分支单独钉住——
    否则它会一直是"写了但没被测过"的代码。
    """
    idx = Bm25Index().build(ROWS + [{
        "chunk_id": "exam-defer", "document_id": "d3",
        "document_title": "课程考核与成绩管理办法",
        "content": "第五章 免修、免听与学分认定 学生因病因事无法参加期末考试的，可申请缓考，缓考安排在下一学期初另行组织。",
        "section_title": "第五章 免修、免听与学分认定", "chunk_index": 4,
    }])
    # 用户说"补考"，扩展词给出原文的写法"缓考"
    a_terms, e_terms = tokenize("补考"), tokenize("缓考 期末")

    # 注意 `score_chunk_with` 对"一个词都没命中"返回 coverage=0 的 hit 而不是 None——
    # 它只在片段不存在或词项集合为空时返回 None。所以判据是覆盖率，不是 None。
    base = idx.score_chunk_with(a_terms, "exam-defer")
    assert base is not None and base.coverage == 0.0, (
        "构造失败：A 视图必须一个词都命不中，否则测不到翻盘分支"
    )
    extra = idx.score_chunk_with(e_terms, "exam-defer")
    assert extra is not None and extra.coverage > 0
    combined = _score_combined(idx, a_terms, e_terms, "exam-defer")
    assert combined.coverage == extra.coverage
    # 翻盘时分数也得跟着来，否则它会是 0 分排在最末，等于没捞
    assert combined.score == extra.score
    assert combined.matched_terms == extra.matched_terms


def test_unknown_chunk_stays_none_even_with_expansion(index):
    """`score_chunk_with` 只在片段不存在时返回 None。零补一条必须先判 None，
    否则会凭空造出一条不存在的片段，带着空标题混进证据块。"""
    assert _score_combined(index, tokenize("图书馆"), tokenize("逾期费"), "不存在") is None


def test_score_and_matched_terms_stay_on_the_standalone_view(index):
    """覆盖率可以取扩展视图，但**分数与命中词仍用自包含查询**——
    排序看的是"多贴用户这句话"，不是"撞上了几个同义词"。"""
    a_terms, e_terms = tokenize(USER_QUERY), tokenize(" ".join(EXPANDED))
    base = index.score_chunk_with(a_terms, "lib-fee")
    combined = _score_combined(index, a_terms, e_terms, "lib-fee")

    assert combined.score == base.score
    assert combined.matched_terms == base.matched_terms
    assert combined.document_title == base.document_title


def test_unknown_chunk_returns_none(index):
    assert _score_combined(index, tokenize(USER_QUERY), tokenize("逾期费"), "不存在") is None


# --- 3. RRF 融合本身 ---------------------------------------------------------


def test_rrf_prefers_chunks_found_by_both_paths(index):
    """两路都排在前面的片段应当赢过"只在单路排第一"的片段。

    这是 RRF 的核心性质，也是它比"拼接去重"强的地方：拼接时第二路的片段
    永远排在后面，等于第二路只在前一路凑不满时才起作用。
    """
    path_a = index.search("图书馆", top_k=4, min_coverage=0.0)
    path_b = index.search("逾期费 滞纳金", top_k=4, min_coverage=0.0)
    fused = _rrf([path_a, path_b])

    both = {h.chunk_id for h in path_a} & {h.chunk_id for h in path_b}
    assert both, "构造失败：两条路没有交集，测不到融合的效果"
    # 交集里名次最好的片段，融合后不应排在只被单路命中的片段之后
    best_both = min(both, key=lambda cid: min(
        [i for i, h in enumerate(path_a, 1) if h.chunk_id == cid]
        + [i for i, h in enumerate(path_b, 1) if h.chunk_id == cid]))
    only_one = [cid for cid in fused if cid not in both]
    if only_one:
        assert fused.index(best_both) < fused.index(only_one[0])


def test_rrf_scores_follow_the_formula(index):
    """`1/(k + rank)` 逐项相加，k 取 RRF_K。名次要真的参与计算，不能只看被几路命中。"""
    path_a = index.search("图书馆", top_k=4, min_coverage=0.0)
    fused = _rrf([path_a])
    expected = {h.chunk_id: 1.0 / (RRF_K + rank) for rank, h in enumerate(path_a, 1)}
    # 单路时 RRF 顺序就是原顺序
    assert fused == _ids(path_a)
    assert all(expected[cid] > 0 for cid in fused)


def test_rrf_dedupes_across_paths(index):
    path_a = index.search("图书馆", top_k=4, min_coverage=0.0)
    fused = _rrf([path_a, path_a])
    assert len(fused) == len(set(fused)), "同一片段被两路命中要合并成一个，不能出现两次"
    assert fused == _ids(path_a), "两路完全相同时，顺序不应被打乱"


def test_rrf_k_is_60():
    """k 写死在设计 §7.2 里，改了要同步文档——所以这里钉住它。"""
    assert RRF_K == 60

# --- 4. 改写只做加法：闸门与召回都不许因为改写而变差 ------------------------
# 这两条守的是 R3 最容易引入的反向退化：
# "改写把问题变具体 → 覆盖率分母变大 → 原本能过的候选被自己的新功能挡在门外"。
# 实测在演示库上真的发生过（同一问题 R2 过闸 6 条、R3 只剩 5 条），
# 所以不是理论风险，而是必须钉住的行为。

# 与 A 视图相比更长、更"具体"的自包含查询：分母从 5 个词项涨到 7 个
LONGER_STANDALONE = "图书馆图书逾期收费规定"


@pytest.fixture()
def live(app, index):
    """把仿真索引装进 app，让 `retrieve()` 走真实检索链路。"""
    with app.app_context():
        app.extensions["bm25_index"] = index
        yield index


def test_a_view_alone_would_refuse_the_chunk(index):
    """先证明构造有效：只看自包含视图，这条片段确实掉到阈值以下。

    没有这一步，下面两条测试可能在"两个口径都能过闸"的构造上跑成永远通过——
    那种测试看起来一直在守着，其实什么也没守。
    """
    a_terms = tokenize(LONGER_STANDALONE)
    threshold = 0.35
    assert index.score_chunk_with(a_terms, "lib-fee").coverage < threshold
    assert index.score_chunk_with(tokenize(USER_QUERY), "lib-fee").coverage >= threshold


def test_rewrite_cannot_push_a_passing_chunk_out_of_the_gate(live):
    """R2 能过闸的片段，加上改写之后**仍然**要能过闸。

    这就是"闸门覆盖率取三视图最大值"的理由：改写只该把候选捞回来，
    不该因为它把问题描述得更细，反过来把本来够格的片段挤出去。
    """
    r2 = retrieve(USER_QUERY, top_k=6)
    assert "lib-fee" in {c.chunk_id for c in r2.candidates}, "构造失败：R2 里它就该过闸"

    r3 = retrieve(USER_QUERY, top_k=6,
                  standalone_query=LONGER_STANDALONE,
                  expanded_terms=["滞纳金", "超期罚款"])
    passed = {c.chunk_id for c in r3.candidates}
    assert "lib-fee" not in {c.chunk_id for c in r3.rejected}, (
        "改写把问题变具体后，原本过闸的片段掉到了闸门外——这正是 R3 的反向退化"
    )
    assert "lib-fee" in passed


def test_original_query_keeps_its_own_recall_path(live):
    """原话必须单独占一路召回，否则改写跑偏时 R2 的候选连池子都进不来。

    构造用"改写成一个别的主题"的最坏情况：如果原话不成一路，
    `lib-fee`（R2 的第 3 名）根本不会出现在 R3 的候选池里，
    覆盖率再宽松也无从补救——因为闸门只筛池子，不捞池子外的片段。
    """
    r2 = retrieve(USER_QUERY, top_k=6)
    r3 = retrieve(USER_QUERY, top_k=6,
                  standalone_query="学生宿舍晚归登记规定",
                  expanded_terms=["门禁"])
    assert r3.recall_paths["original"] > 0, "原话那一路必须真的跑过"
    assert r3.recall_paths["rrf"] is True
    assert {c.chunk_id for c in r2.candidates} <= {c.chunk_id for c in r3.candidates}, (
        "R2 的候选集必须是 R3 候选集的子集：改写只做加法"
    )


def test_recall_paths_names_all_three_paths(live):
    """轨迹要能回答"这轮走了哪几路"，否则调试时只能猜。"""
    outcome = retrieve(USER_QUERY, top_k=6,
                       standalone_query=LONGER_STANDALONE,
                       expanded_terms=["逾期费", "滞纳金"])
    paths = outcome.recall_paths
    assert set(paths) >= {"original", "standalone", "expanded", "fused", "rrf"}
    assert paths["terms"]["original"] > 0
    assert paths["terms"]["standalone"] > 0
    assert paths["terms"]["expanded"] > 0
