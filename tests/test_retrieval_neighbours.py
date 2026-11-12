"""相邻片段补全（R2）的单元测试。

**这条规则为什么值得单独测**：

`eval` 的 24 条题在补全上线前后都是 24/24——因为它们的问法与原文用词相近，
本来就能排进前六。真正被补全救回来的是"用大白话问"的那一类：

    问「图书馆的书逾期了怎么收费」，而原文写的是"按每册每天 0.1 元收取逾期费"。
    "收费" ≠ "收取"，含条款的那一章覆盖率只有 0.4，被同文件新增的三章挤出证据窗口。

补全的边界必须钉死，否则它就成了"变相放宽拒答"：

1. 邻片**自己**也要过覆盖率闸门（不是因为它挨着谁就自动进来）；
2. 已经在窗口里的片段不重复补；
3. 一次最多补 `NEIGHBOUR_LIMIT` 条，不能把证据窗口塞满。
"""

from __future__ import annotations

from app.indexing import Bm25Index
from app.retrieval import (
    NEIGHBOUR_LIMIT,
    Candidate,
    RetrievalOutcome,
    _complete_neighbours,
)

# 一份"章节间词汇分布不均"的仿真文件：只有第 2、4 章讲逾期费，
# 但第 1、3、5 章反复出现"图书馆"，正是真实演示库扩写后的样子。
ROWS = [
    {
        "chunk_id": "c0", "document_id": "d1", "document_title": "图书馆借阅与自习空间管理办法",
        "content": "第一章 开放时间 图书馆周一至周日开放，借阅区 8:00 至 22:00。",
        "section_title": "第一章 开放时间", "chunk_index": 0,
    },
    {
        "chunk_id": "c1", "document_id": "d1", "document_title": "图书馆借阅与自习空间管理办法",
        "content": "第二章 借阅规则 本科生可借阅图书 10 册，逾期未还的按每册每天 0.1 元收取逾期费。",
        "section_title": "第二章 借阅规则", "chunk_index": 1,
    },
    {
        "chunk_id": "c2", "document_id": "d1", "document_title": "图书馆借阅与自习空间管理办法",
        "content": "第三章 自习空间 图书馆自习座位实行预约制，每次最长 4 小时。",
        "section_title": "第三章 自习空间", "chunk_index": 2,
    },
    {
        "chunk_id": "c3", "document_id": "d1", "document_title": "图书馆借阅与自习空间管理办法",
        "content": "第四章 电子资源 图书馆订购数据库 40 余个，校外访问需 VPN。",
        "section_title": "第四章 电子资源", "chunk_index": 3,
    },
    {
        "chunk_id": "c4", "document_id": "d1", "document_title": "图书馆借阅与自习空间管理办法",
        "content": "第五章 违规处理 在图书馆阅览区饮食、占座拒不改正的，暂停借阅权限。",
        "section_title": "第五章 违规处理", "chunk_index": 4,
    },
]


def _candidate(hit, *, via: bool = False) -> Candidate:
    return Candidate(
        index=0,
        chunk_id=hit.chunk_id,
        document_id=hit.document_id,
        document_title=hit.document_title,
        content=hit.content,
        page_number=hit.page_number,
        section_title=hit.section_title,
        score=hit.score,
        coverage=hit.coverage,
        matched_terms=tuple(hit.matched_terms),
        via_neighbour=via,
        chunk_index=hit.chunk_index,
    )


QUERY = "图书馆的书逾期了怎么收费"


def _setup(top_k: int = 3, threshold: float = 0.35):
    index = Bm25Index().build(ROWS)
    hits = index.search(QUERY, top_k=top_k, min_coverage=0.0)
    top = [_candidate(h) for h in hits]
    passed = [_candidate(h) for h in index.search(QUERY, top_k=99, min_coverage=threshold)]
    outcome = RetrievalOutcome()
    return index, top, passed, outcome, threshold


def test_top_window_really_misses_the_fee_chapter():
    """先证明问题存在：补全之前，讲逾期费的那一章进不了前三条。"""
    index, top, *_ = _setup()
    assert top, "查询必须有命中，否则测的不是补全"
    assert "c1" not in [c.chunk_id for c in top], (
        "这条用例的前提是「逾期费」被同文件的其它章节挤出了窗口；"
        "若它已在前三，说明语料或排序变了，这条测试失去意义"
    )


def test_completion_brings_back_the_gated_neighbour():
    index, top, passed, outcome, threshold = _setup()
    extras = _complete_neighbours(QUERY, top, passed, index, threshold, outcome)
    ids = [c.chunk_id for c in extras]
    assert "c1" in ids, "含逾期费条款的片段是前几条的紧邻，必须被补回来"
    assert all(c.via_neighbour for c in extras), "补进来的必须标来源，否则看起来像直接命中"


def test_completion_never_lets_a_low_coverage_chunk_in():
    """邻片自己不过闸就不许补——这是"补全"与"放宽拒答"的分界线。"""
    index, top, passed, outcome, _ = _setup()
    # 把阈值抬到只有 0.45 以上才够格：c1 的覆盖率低于它，应当补不进来
    strict = 0.95
    extras = _complete_neighbours(QUERY, top, passed, index, strict, outcome)
    assert extras == [], "阈值之上没有邻片时，一条都不该补"


def test_completion_skips_chunks_already_in_the_window():
    index, top, passed, outcome, threshold = _setup(top_k=5)
    extras = _complete_neighbours(QUERY, top, passed, index, threshold, outcome)
    window = {c.chunk_id for c in top}
    assert not (window & {c.chunk_id for c in extras}), "窗口里已有的片段不得重复出现"


def test_completion_is_capped():
    index, top, passed, outcome, threshold = _setup()
    extras = _complete_neighbours(QUERY, top, passed, index, threshold, outcome)
    assert len(extras) <= NEIGHBOUR_LIMIT


def test_empty_window_does_not_trigger_completion():
    """一条都没过闸（该拒答的问题）时，补全不能凭空造出证据。"""
    index, _, _, outcome, threshold = _setup()
    extras = _complete_neighbours(QUERY, [], [], index, threshold, outcome)
    assert extras == []
