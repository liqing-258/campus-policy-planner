"""检索服务：把索引层的原始命中，加工成问答链路能用的"带编号证据"。

为什么要有这一层，而不是让 qa.py 直接调 Bm25Index.search()：

1. **编号化**。模型只能看到 `[1] [2] [3]`，看不到 chunk_id。
   这样模型**在物理上无法编造一个不存在的 chunk_id**——它只能给出越界编号，
   而越界编码是可以 100% 确定性检出的（`n > len(candidates)`）。
   反过来，如果让模型直接输出 chunk_id，编造与真实在字符串层面长得一模一样，
   校验只能靠"查库比对"，而校验失败时无法区分"模型编造"与"切分后 ID 变了"。

2. **拒答闸门只放在一处**。覆盖率阈值如果散落在路由和问答两处，迟早不一致。

3. **拒答要带证据**。拒答时把"被过滤掉的候选"一起回传，
   用户才能判断到底是"库里真没有"还是"阈值定太高了"。
   只回一句"证据不足"，把排查成本全推给用户，这是偷懒。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.indexing import ensure_index_loaded, get_index
from app.runtime import current_settings
from shared.enums import EMBEDDING_BM25_ONLY, EMBEDDING_VECTOR


@dataclass
class Candidate:
    """一条候选证据。`index` 是给模型和前端看的 1-based 编号。"""

    index: int
    chunk_id: str
    document_id: str
    document_title: str
    content: str
    page_number: int | None
    section_title: str | None
    score: float
    coverage: float
    matched_terms: tuple[str, ...] = ()

    def to_public(self, *, with_content: bool = True) -> dict:
        """对外序列化。内容可截断——列表场景不需要整段正文，详情接口才需要。"""
        payload = {
            "index": self.index,
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "document_title": self.document_title,
            "page_number": self.page_number,
            "section_title": self.section_title,
            "score": self.score,
            "coverage": self.coverage,
        }
        if with_content:
            payload["content"] = self.content
        return payload


@dataclass
class RetrievalOutcome:
    candidates: list[Candidate] = field(default_factory=list)
    # 过闸前的全部命中，用于拒答时回传给用户
    rejected: list[Candidate] = field(default_factory=list)
    total_hits: int = 0
    mode: str = EMBEDDING_BM25_ONLY
    refused: bool = False
    refuse_reason: str | None = None
    min_coverage: float = 0.0
    top_coverage: float | None = None
    index_size: int = 0

    def to_public(self, *, with_content: bool = True) -> dict:
        return {
            "mode": self.mode,
            "min_coverage": self.min_coverage,
            "candidates": [c.to_public(with_content=with_content) for c in self.candidates],
            "total_hits": self.total_hits,
            "index_size": self.index_size,
            "refused": self.refused,
            "refuse_reason": self.refuse_reason,
        }


def retrieval_mode() -> str:
    """如实返回检索模式。未配置 embedding 时就是 bm25_only，不粉饰成 hybrid。"""
    settings = current_settings()
    if settings.embedding_api_key and settings.embedding_model:
        return EMBEDDING_VECTOR
    return EMBEDDING_BM25_ONLY


def retrieve(query: str, *, top_k: int | None = None,
             document_ids: list[str] | None = None,
             min_coverage: float | None = None) -> RetrievalOutcome:
    """检索并施加覆盖率闸门。

    两段式：先不过闸取回全部命中（含低覆盖率的），再按阈值切分。
    这样拒答时能告诉用户"我看到了 3 条候选，但覆盖率都只有 0.2，所以不敢回答"。
    """
    settings = current_settings()
    limit = top_k if top_k is not None else settings.rag_top_k
    threshold = settings.rag_min_coverage if min_coverage is None else min_coverage

    # 索引在内存里，进程刚启动时是空的。不先懒加载，"库里有 4 份文件"也会被报成"知识库为空"。
    ensure_index_loaded()
    index = get_index()

    outcome = RetrievalOutcome(
        mode=retrieval_mode(),
        min_coverage=threshold,
        index_size=index.size,
    )

    if not index.is_built or index.size == 0:
        outcome.refused = True
        outcome.refuse_reason = "知识库为空：尚未上传任何已解析的政策文件，请先上传后再提问"
        return outcome

    # min_coverage 传 0：这一层的闸门由本函数自己施加，索引层不再重复过滤
    hits = index.search(query, top_k=max(limit * 3, limit), document_ids=document_ids,
                        min_coverage=0.0)
    outcome.total_hits = len(hits)
    if not hits:
        outcome.refused = True
        outcome.refuse_reason = "没有检索到任何与问题词项相关的片段"
        return outcome

    outcome.top_coverage = max(h.coverage for h in hits)

    passed: list[Candidate] = []
    rejected: list[Candidate] = []
    for position, hit in enumerate(hits, 1):
        candidate = Candidate(
            index=position,
            chunk_id=hit.chunk_id,
            document_id=hit.document_id,
            document_title=hit.document_title,
            content=hit.content,
            page_number=hit.page_number,
            section_title=hit.section_title,
            score=hit.score,
            coverage=hit.coverage,
            matched_terms=tuple(hit.matched_terms),
        )
        if hit.coverage >= threshold:
            passed.append(candidate)
        else:
            rejected.append(candidate)

    # 重新编 1..N：模型看到的编号必须连续，否则"编号越界"的判定会失去意义
    for new_index, candidate in enumerate(passed[:limit], 1):
        candidate.index = new_index
    outcome.candidates = passed[:limit]
    outcome.rejected = rejected[:limit]

    if not outcome.candidates:
        outcome.refused = True
        outcome.refuse_reason = (
            f"命中 {outcome.total_hits} 条片段，但最高关键词覆盖率仅 "
            f"{outcome.top_coverage:.2f}，低于阈值 {threshold}，判为主题不相关"
        )
    return outcome
