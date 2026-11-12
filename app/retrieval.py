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

from dataclasses import dataclass, field, replace

from app import injection, versions
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
    # 疑似提示注入的命中模式（M6 / T-11）。空元组 = 没发现疑点。
    injection_hits: tuple[dict, ...] = ()
    # 降权前的覆盖率。只有命中注入模式时才有值——否则 `coverage` 就是原值，
    # 多存一个字段只会让人怀疑"是不是还有别的地方也在改分数"。
    raw_coverage: float | None = None
    # 不是检索直接命中的，而是作为命中片段的"紧邻片段"补进来的。
    # 为什么要标出来：它同样过了覆盖率闸门，但排序上被别的片段挤掉了——
    # 标出来，评审追问"这条哪来的"时有话可说，而不是看起来像凭空多出来的证据。
    via_neighbour: bool = False
    # 片段在文件内的序号，补邻片时用它找前后邻居
    chunk_index: int = 0

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
            # 前端不必为它加图标，但接口里要能看出来：这条是"紧邻补全"，不是直接命中
            "via_neighbour": self.via_neighbour,
        }
        if self.injection_hits:
            payload["injection"] = {
                "suspected": True,
                "hits": list(self.injection_hits),
                "original_coverage": self.raw_coverage,
                "weight": injection.INJECTION_WEIGHT,
                "note": "该片段含指令式文字，已按系数降权；它仍可能是政策原文，请核对后再采信",
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
    # M6：同一主题的多版本（T-12）与疑似注入汇总（T-11）
    conflicts: list[dict] = field(default_factory=list)
    injection_suspected: list[dict] = field(default_factory=list)
    # R2：补进来的相邻片段条数（0 表示本轮没触发）
    neighbour_added: int = 0

    def to_public(self, *, with_content: bool = True) -> dict:
        return {
            "mode": self.mode,
            "min_coverage": self.min_coverage,
            "candidates": [c.to_public(with_content=with_content) for c in self.candidates],
            "total_hits": self.total_hits,
            "index_size": self.index_size,
            "refused": self.refused,
            "refuse_reason": self.refuse_reason,
            "conflicts": self.conflicts,
            "injection_suspected": self.injection_suspected,
        }


def retrieval_mode() -> str:
    """如实返回检索模式。未配置 embedding 时就是 bm25_only，不粉饰成 hybrid。"""
    settings = current_settings()
    if settings.embedding_api_key and settings.embedding_model:
        return EMBEDDING_VECTOR
    return EMBEDDING_BM25_ONLY


# ---------- 相邻片段补全 ----------
# 补几个父片段的邻居、最多补几条。数字不大是刻意的：
# 补全只解决"条款被同文件的其它章节挤出窗口"，不负责把证据塞满。
NEIGHBOUR_PARENTS = 3
NEIGHBOUR_LIMIT = 3


def _complete_neighbours(query: str, top: list[Candidate], passed: list[Candidate],
                         index, threshold: float,
                         outcome: RetrievalOutcome) -> list[Candidate]:
    """把"过了闸门但被排序挤掉"的相邻片段补进证据。

    为什么需要这一步（R2 实测的现场）：

        《图书馆借阅与自习空间管理办法》补到 7 个片段后，问
        「图书馆的书逾期了怎么收费」，含逾期费条款的「第二章 借阅规则」
        只覆盖 2/5 个查询词（0.4），而新增的电子资源、座位管理几章覆盖 0.6/0.8。
        `search()` 先按覆盖率排序，于是第二章排到第 11 位，前 6 条里没有它，
        模型看到六段都不讲钱，只能拒答——而原文白纸黑字写着 0.1 元/册/天。

    它和"降阈值"的区别：**闸门一条没松**。补进来的片段必须自己也算出
    `coverage >= threshold`，也就是"本来就够格、只是没抢到座"。
    真正该拒的问题（库里没有）不会有任何片段过闸，这一步就不会触发。
    """
    if not top:
        return []
    # 只排除"已经进了证据窗口"的片段。注意不能拿整个 passed 当排除表——
    # 被补的邻片本来就在 passed 里（它过了闸、只是排序靠后），
    # 早先按 passed 排除，结果一条都补不进来，neighbour_added 恒为 0。
    taken = {c.chunk_id for c in top}
    already = {c.chunk_id: c for c in passed}
    extras: dict[str, Candidate] = {}
    for parent in top[:NEIGHBOUR_PARENTS]:
        for chunk in index.neighbours(parent.document_id, parent.chunk_index):
            if chunk.chunk_id in taken:
                continue
            reused = already.get(chunk.chunk_id)
            if reused is not None:
                # 已经在 passed 里：沿用它的注入标记与覆盖率，只补一个"来源"标记
                candidate = replace(reused, via_neighbour=True)
            else:
                hit = index.score_chunk(query, chunk.chunk_id)
                if hit is None or hit.score <= 0:
                    continue
                found = injection.scan(hit.content)
                coverage = injection.downweight(hit.coverage) if found else hit.coverage
                if found:
                    outcome.injection_suspected.append({
                        "chunk_id": hit.chunk_id,
                        "document_title": hit.document_title,
                        "patterns": [h["code"] for h in found],
                        "original_coverage": round(hit.coverage, 4),
                        "adjusted_coverage": coverage,
                        "kept": True,
                        "via": "neighbour",
                    })
                candidate = Candidate(
                    index=0,
                    chunk_id=hit.chunk_id,
                    document_id=hit.document_id,
                    document_title=hit.document_title,
                    content=hit.content,
                    page_number=hit.page_number,
                    section_title=hit.section_title,
                    score=hit.score,
                    coverage=coverage,
                    matched_terms=tuple(hit.matched_terms),
                    injection_hits=tuple(found),
                    raw_coverage=hit.coverage if found else None,
                    via_neighbour=True,
                    chunk_index=hit.chunk_index,
                )
            if candidate.coverage < threshold:
                continue  # 邻片自己也得过闸，不然补进来的就是"凑数证据"
            taken.add(chunk.chunk_id)
            extras[chunk.chunk_id] = candidate
    ordered = sorted(extras.values(), key=lambda c: (c.coverage, c.score), reverse=True)
    return ordered[:NEIGHBOUR_LIMIT]


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
        found = injection.scan(hit.content)
        # 疑似注入就降权，而不是丢弃：高相关片段（≥0.8）降权后仍在阈值之上，
        # 它会带着标记进入证据块——这样既没隐藏政策原文，又让拦截动作可见。
        coverage = injection.downweight(hit.coverage) if found else hit.coverage
        candidate = Candidate(
            index=position,
            chunk_id=hit.chunk_id,
            document_id=hit.document_id,
            document_title=hit.document_title,
            content=hit.content,
            page_number=hit.page_number,
            section_title=hit.section_title,
            score=hit.score,
            coverage=coverage,
            matched_terms=tuple(hit.matched_terms),
            injection_hits=tuple(found),
            raw_coverage=hit.coverage if found else None,
            chunk_index=hit.chunk_index,
        )
        if found:
            outcome.injection_suspected.append({
                "chunk_id": hit.chunk_id,
                "document_title": hit.document_title,
                "patterns": [h["code"] for h in found],
                "original_coverage": round(hit.coverage, 4),
                "adjusted_coverage": coverage,
                "kept": coverage >= threshold,
            })
        if coverage >= threshold:
            passed.append(candidate)
        else:
            rejected.append(candidate)

    # 重新编 1..N：模型看到的编号必须连续，否则"编号越界"的判定会失去意义
    top = passed[:limit]
    extras = _complete_neighbours(query, top, passed, index, threshold, outcome)
    for new_index, candidate in enumerate(top + extras, 1):
        candidate.index = new_index
    outcome.candidates = top + extras
    outcome.rejected = rejected[:limit]
    outcome.neighbour_added = len(extras)

    if not outcome.candidates:
        outcome.refused = True
        outcome.refuse_reason = (
            f"命中 {outcome.total_hits} 条片段，但最高关键词覆盖率仅 "
            f"{outcome.top_coverage:.2f}，低于阈值 {threshold}，判为主题不相关"
        )

    # 版本冲突在闸门之后判：被拒答时也要带上（"你问的这个主题库里有 2025 版"），
    # 所以候选为空时退回用被拦下的片段来判。
    outcome.conflicts = versions.detect(outcome.candidates or outcome.rejected)
    return outcome
