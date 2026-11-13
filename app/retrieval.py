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

from app import anchors, injection, versions
from app.indexing import SearchHit, ensure_index_loaded, get_index, tokenize
from app.runtime import current_settings
from shared.enums import EMBEDDING_BM25_ONLY, EMBEDDING_VECTOR

# RRF 的平滑常数。k=60 是原论文与设计 §7.2 共同采用的值——
# 它把"第 1 名"与"第 2 名"的差距压到很小，使得单路里的绝对名次
# 不会因为另一路完全没召回而获得压倒性优势。
RRF_K = 60


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
    # 不是检索命中，而是用户用《文件名》点名了这份文件、问法又是"还有哪些规定"时，
    # 按文件顺序摆出来的片段。**覆盖率仍然如实计算**（多半很低），不篡改数字——
    # 标出来是为了让"这条为什么在证据里"可回答。
    via_anchor: bool = False
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
            "via_anchor": self.via_anchor,
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
    # R3：查询改写与双路召回。没启用改写时 standalone_query 就是原问题、
    # expanded_terms 为空，这几项如实反映"这轮走的是单路"。
    standalone_query: str = ""
    expanded_terms: tuple[str, ...] = ()
    # 两路各召回多少条、融合后候选池多大、三个词项视图各几个词项。
    # 用于解释"为什么这次多出来几条"以及"改写到底改了什么"。
    recall_paths: dict = field(default_factory=dict)
    # 用户用《文件名》点名了库内文件时的锚定信息（见 app/anchors.py）。
    # `anchor_gate` 只在一种情况下有值：点名 + 残余问法无主题词 → 闸门不适用。
    anchor: dict | None = None
    anchor_gate: str | None = None

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
            # R3：前端调试面板与接口调用方都能看出"这轮用了哪一路"
            "standalone_query": self.standalone_query,
            "expanded_terms": list(self.expanded_terms),
            "recall_paths": self.recall_paths,
            # R4：问句里点名了哪份文件、摘掉书名号后的问法、闸门是否适用
            "anchor": self.anchor,
            "anchor_gate": self.anchor_gate,
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
                         outcome: RetrievalOutcome,
                         a_terms: list[str] | None = None,
                         e_terms: list[str] | None = None,
                         o_terms: list[str] | None = None) -> list[Candidate]:
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
                # 打分必须与主路径同口径：主路径用的是"三个词项视图取覆盖更高者"，
                # 这里若拿原问题单独去算，邻片的覆盖率就换了一把尺子。
                hit = (_score_combined(index, a_terms, e_terms or [], chunk.chunk_id, o_terms or ())
                       if a_terms else index.score_chunk(query, chunk.chunk_id))
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


def _score_combined(index, a_terms: list[str], e_terms: list[str], chunk_id: str,
                    o_terms: list[str] | tuple = ()):
    """同一条片段在三个词项视图下各打一次分，**闸门用的覆盖率取最高者**。

    三个视图分别是：

    | 记号 | 视图 | 来源 | 作用 |
    | --- | --- | --- | --- |
    | `o` | 用户原话 | `question` | **兜底**：保证改写不会让结果比不改写更差 |
    | `a` | 自包含查询 | `standalone_query` | 主视图：排序分数用它算 |
    | `e` | 同义扩展词 | `expanded_terms` | 捞回字面不接轨的片段 |

    **为什么不是把 `a` 与 `e` 的并集当分母（需求书的原做法）**

    并集把扩展词当成"追加要求"（AND）——一条片段除了覆盖用户原话，
    还得分担"滞纳金""借阅管理办法"这几个同义词才不掉分。可扩展词是同义**替代**（OR），
    不是必须同时满足的条件。演示库上实测（`.workbuddy/measure_coverage2.py`）：

        「《图书馆借阅与自习空间管理办法》里还有哪些与这个问题相关的规定？」
            第一章 开放时间    单视图 0.53 → 并集口径 0.40
            第四章 违规处理    单视图 0.40 → 并集口径 0.30 ← 掉到 0.35 以下
            第六章 座位管理    单视图 0.53 → 并集口径 0.40

    阈值 0.35 是 24 条评测集 5 点扫描定的（设计 §7.2.1），分母一变它的含义就变了。
    §7.2.1 的结论恰恰是"闸门应当偏宽松，拒答由模型自评完成"，
    所以这里选 OR 语义：**谁覆盖得好用谁**，扩展词只负责把候选捞回来，不负责扣分。

    **为什么还要第三个 `o` 视图（这一版修的缺陷）**

    改写把问题变长、变具体，`a` 视图的分母随之变大——同一条片段在 `a` 下的覆盖率
    可能低于它在原话下的覆盖率。于是出现一种反向退化：**加了改写，反而把原本能过闸的
    候选挡在门外**。实测案例一在 R2 下过闸 6 条、R3 下只剩 5 条就是这一原因。

    这不是阈值问题，而是"改写只该做加法"这条语义被破坏了。把原话视图并进闸门后，
    得到一条可断言的性质：**任何在 R2 里过闸的片段，在 R3 里仍然过闸**
    （覆盖率取三者最大值，原话视图那一项与 R2 完全相同）。测试
    `tests/test_dual_recall.py::test_rewrite_never_makes_the_gate_stricter` 守着它。

    排序分数仍按自包含查询算：排序看的是"这条片段有多贴改写后的这句话"，
    不是"撞上了几个同义词"，也不是"原话里蒙对了几个词"。原话只影响**进不进门**，
    不影响**排第几**——否则 R3 的排序会退回 R2，改写的意义就没了。

    例外：自包含视图一个词都没命中（`coverage == 0`，改写把问题带偏了的情形），
    这时它的分数是 0，拿它排序等于让这条片段垫底。依次退回原话、扩展视图的分数。

    **不改 `base is None` 的情况**：`score_chunk_with` 只在片段不存在或词项集合为空时
    返回 None。片段不存在就返回 None，不能凭空造出一条不存在的片段；
    但某个视图的词项集合为空（比如自包含查询被全会话历史"讲空"了）只说明那个视图
    给不出信息，不该连累另外两个视图。
    """
    views: dict[str, SearchHit] = {}
    if a_terms:
        found = index.score_chunk_with(a_terms, chunk_id)
        if found is not None:
            views["standalone"] = found
    if o_terms:
        found = index.score_chunk_with(list(o_terms), chunk_id)
        if found is not None:
            views["original"] = found
    if e_terms:
        found = index.score_chunk_with(e_terms, chunk_id)
        if found is not None:
            views["expanded"] = found
    # 三个视图全空 = 片段不存在（词项为空的那些视图已经在上面的 if 里被排除）
    if not views:
        return None

    # 排序用哪个视图的分数：依次找"有命中"的主视图，都没有命中就取第一个存在的
    order = ("standalone", "original", "expanded")
    primary = next((views[k] for k in order if k in views and views[k].coverage > 0), None)
    if primary is None:
        primary = next(views[k] for k in order if k in views)

    best = max(views.values(), key=lambda hit: (hit.coverage, hit.score))
    return replace(best, score=primary.score, matched_terms=primary.matched_terms)


def _path_uncovered(chunk_id: str, paths: list[list[SearchHit]]) -> bool:
    """该片段在**所有**召回路径里都是零命中。

    正常路径下这条恒为 False：融合池里的每个 chunk_id 都来自某一路 `search()` 的返回，
    而 `search()` 已经滤掉 `score <= 0`（一个词都没命中）的片段。也就是说，
    "覆盖率 0 的片段混进证据块"在当前构造里不可能发生。

    留着它是因为**这是一条不变量，不是一段可达逻辑**：将来若有人给融合池加第三路
    （比如向量召回，允许零字面命中），这个判据就是唯一还能拦住噪声的地方。
    早先的写法把"实测 9 条过闸只有 5 条命中"当成它存在的理由——那个现象在
    `_score_combined` 改成三视图取最大值之后就消失了，继续那样写等于拿不存在的数据
    当依据。这里如实改成防御性断言。
    """
    return not any(hit.chunk_id == chunk_id for hits in paths for hit in hits)


def _rrf(paths: list[list[SearchHit]]) -> list[str]:
    """RRF 融合（`score = Σ 1/(k + rank)`），返回按融合分降序的 chunk_id。

    为什么不直接拼接去重：拼接意味着先到先得，第二路召回的片段永远排在后面，
    等于第二路只在"第一路凑不满"时才起作用。RRF 只看名次、不看分值，
    正好绕开"两路的词项集合不同、分数量纲虽同但不可比"的问题。
    """
    scores: dict[str, float] = {}
    for hits in paths:
        for rank, hit in enumerate(hits, 1):
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1.0 / (RRF_K + rank)
    # 只按分数排；同分时保持插入顺序（第一路的先出现）
    return sorted(scores, key=lambda chunk_id: scores[chunk_id], reverse=True)


# 锚定填充时 `anchor_gate` 的取值。写成常量而不是散落的字符串字面量，
# 因为它是对外契约的一部分（会出现在 `retrieval.anchor_gate` 与节点轨迹里）。
ANCHOR_GATE_DOCUMENT = "document_scoped"


def _fill_by_anchor(outcome: RetrievalOutcome, index, anchor, limit: int,
                    a_terms: list[str], e_terms: list[str],
                    o_terms: list[str]) -> None:
    """点名了文件：证据窗口 = 被点名文件的片段。

    **为什么这里不让覆盖率闸门说话**

    闸门回答的问题是"用户问的这个主题，库里有没有"。用户写了《文件名》之后，
    这个问题已经被"那份文件在库里"回答了；残余问法里的词只是**在文件内部指路**，
    不再是"库里有没有这个主题"的判据。

    最直接的证据是前端自己生成的三条追问模板，它们对同一份文件都成立：

        《X》里还有哪些与这个问题相关的规定？      （残余：还有/问题/规定，无主题词）
        《X》对办理流程和时间要求是怎么规定的？    （残余：办理/流程/时间/要求）
        不符合《X》规定的情况会怎么处理？          （残余：符合/情况/处理）

    第三条里"处理"在《图书馆…办法》中只覆盖到 0.2——按闸门判就该拒答，
    可它问的明明是"这份文件怎么规定处理方式"，答不出来是检索的错，不是用户问错了。
    统一豁免之后，三条模板走同一条路径：**把点名文件里最相关的片段摆出来**，
    再由闸二（模型自评证据充分性）判断能不能答。§7.2.1 已经定过这个分工：
    覆盖率只做粗筛，拒答由模型判定。

    代价说清楚：问"《图书馆办法》里关于转专业的规定"，也会把图书馆的片段摆出来
    （模型会说证据不足，降级模式只列原文）。这比"另一个文件里有转专业，就答
    另一个文件"要正确——**用户点名了哪份文件，就答哪份文件**。

    **排序**：开放式问法（摘净后没有主题词）按文件内顺序，因为这时没有可比的相关度，
    用户要的是"这份文件讲了什么"；有主题词时按 BM25 与覆盖率排，让相关章节靠前。
    """
    scored: list[tuple[int, Candidate]] = []
    doc_order = {doc_id: pos for pos, doc_id in enumerate(anchor.document_ids)}
    for document_id in anchor.document_ids:
        for chunk in index.chunks_of(document_id):
            hit = _score_combined(index, a_terms, e_terms, chunk.chunk_id, o_terms)
            found = injection.scan(chunk.content)
            if hit is not None:
                coverage = injection.downweight(hit.coverage) if found else hit.coverage
                score = hit.score
                matched = tuple(hit.matched_terms)
                raw = hit.coverage if found else None
            else:
                # 残余问法一个词项都没有（用户就写了"《X》"）：无可计算的分与覆盖率。
                # 给 0 而不是"看起来还行"的默认值——数字要能被追问。
                coverage, score, matched, raw = 0.0, 0.0, (), None
            candidate = Candidate(
                index=0,
                chunk_id=chunk.chunk_id,
                document_id=chunk.document_id,
                document_title=chunk.document_title,
                content=chunk.content,
                page_number=chunk.page_number,
                section_title=chunk.section_title,
                score=score,
                coverage=coverage,
                matched_terms=matched,
                injection_hits=tuple(found),
                raw_coverage=raw,
                via_anchor=True,
                chunk_index=chunk.chunk_index,
            )
            if found:
                outcome.injection_suspected.append({
                    "chunk_id": chunk.chunk_id,
                    "document_title": chunk.document_title,
                    "patterns": [h["code"] for h in found],
                    "original_coverage": round(hit.coverage, 4) if hit is not None else 0.0,
                    "adjusted_coverage": coverage,
                    # 点名文件的片段不会因为注入疑点被丢掉：用户要看的是原文
                    "kept": True,
                    "via": "anchor",
                })
            scored.append((doc_order.get(document_id, 0), candidate))

    outcome.total_hits = len(scored)
    if anchor.open_query:
        # 文件内顺序。**多版本时按章节交错**（第一章×2、第二章×2…）而不是
        # 把 2025 版排完再排 2026 版：窗口只有 6 条，先排完一份就把另一份挤掉了，
        # 而"两版差别在哪"恰恰是用户点名时的真实问题。
        scored.sort(key=lambda item: (item[1].chunk_index, item[0]))
    else:
        scored.sort(key=lambda item: (item[1].coverage, item[1].score), reverse=True)

    picked = [candidate for _, candidate in scored[:limit]]
    for position, candidate in enumerate(picked, 1):
        candidate.index = position
    outcome.candidates = picked
    # **闸门不适用**——不是"放宽阈值"，是把这件事记下来，让轨迹能解释
    outcome.anchor_gate = ANCHOR_GATE_DOCUMENT
    outcome.top_coverage = max((c.coverage for c in picked), default=None)
    outcome.recall_paths = {
        "original": 0, "standalone": 0, "expanded": 0,
        "fused": 0, "rrf": False, "anchored": len(picked),
        "terms": {"original": len(o_terms), "standalone": len(a_terms),
                  "expanded": len(e_terms)},
    }
    if not picked:
        # 点名了文件、但库里那份文件没有任何片段：这是真的没证据。
        outcome.refused = True
        outcome.refuse_reason = "被点名的文件中没有可检索的片段"
        return
    outcome.conflicts = versions.detect(outcome.candidates)


def retrieve(query: str, *, top_k: int | None = None,
             document_ids: list[str] | None = None,
             min_coverage: float | None = None,
             standalone_query: str | None = None,
             expanded_terms: list[str] | None = None) -> RetrievalOutcome:
    """检索并施加覆盖率闸门。

    两段式：先不过闸取回全部命中（含低覆盖率的），再按阈值切分。
    这样拒答时能告诉用户"我看到了 3 条候选，但覆盖率都只有 0.2，所以不敢回答"。

    **R3 起支持双路召回**：给了 `expanded_terms` 就多跑一路"同义扩展词"检索，
    两路用 RRF 融合决定谁进候选池；覆盖率闸门取"用户原话 / 自包含查询 / 扩展词"
    三个视图的最高值（见 `_score_combined`），保证**改写只做加法**。
    不给这两个参数时行为与 R2 完全一致（单路 + 原查询），便于对比与回滚。

    **R4 起识别"点名文件"**：问句里有《文件名》且能对上库内文件时（`app/anchors.py`），
    把书名号换成核心名（去掉年份/版本括号）再检索、把范围收窄到被点名的文件，
    并**不再施加覆盖率闸门**——用户点名了哪份文件，就答哪份文件；能不能答由闸二判。
    这是**确定性规则**，不依赖模型：降级路径（无 API Key）同样生效。
    这一条修的是实测缺陷：问《…（2026年版）》时，2026 版正文里没有"2026"这个词，
    被点名的那份反而被自己的年份压到 0.308，整份出闸门；而 2025 版因为正文印着
    "（2025年版）"排到第一——**系统优先给了旧版本**。
    """
    settings = current_settings()
    limit = top_k if top_k is not None else settings.rag_top_k
    threshold = settings.rag_min_coverage if min_coverage is None else min_coverage

    # 索引在内存里，进程刚启动时是空的。不先懒加载，"库里有 4 份文件"也会被报成"知识库为空"。
    ensure_index_loaded()
    index = get_index()

    standalone = (standalone_query or query or "").strip() or query
    expanded = [term for term in (expanded_terms or []) if term and term.strip()]

    outcome = RetrievalOutcome(
        mode=retrieval_mode(),
        min_coverage=threshold,
        index_size=index.size,
        standalone_query=standalone,
        expanded_terms=tuple(expanded),
    )

    if not index.is_built or index.size == 0:
        outcome.refused = True
        outcome.refuse_reason = "知识库为空：尚未上传任何已解析的政策文件，请先上传后再提问"
        return outcome

    # ---------- 书名号锚定（确定性，先于任何检索） ----------
    # 两处都试：原话里点名最可信；改写可能把文件名改写掉，所以只在原话没锚上时
    # 再看自包含查询——反过来（先看改写）会让"改写删掉了文件名"变成静默失锚。
    anchor = anchors.resolve(query, index.documents(), allowed=document_ids)
    if anchor is None and standalone != query:
        anchor = anchors.resolve(standalone, index.documents(), allowed=document_ids)

    search_query = query
    search_standalone = standalone
    # 调用方给了文档范围就只在范围内检索（知识库页"只看这份文件"用到）。
    # 锚定不走这条路径：它自己的范围由书名号决定，见 `_fill_by_anchor`。
    scope = document_ids
    if anchor is not None:
        # 书名号换成核心名（去掉年份/版本括号）再分词：年份是指针，不是主题词。
        # 留着它，"2026"会进覆盖率分母——而 2026 版正文里根本没有"2026"，
        # 于是被点名的那份文件反而被压低（实测 0.308 < 0.35，整份被挡在门外）。
        # 保留核心名同样重要：里面带着"奖学金/图书馆借阅"这类真主题词。
        search_query = anchor.residual or query
        search_standalone = anchors.residual_text(standalone) or standalone
        # 开放式问法要看**摘净版**：residual 里含这份文件自己的名字，
        # 拿它判"有没有主题词"必然判成有（自我命中）。
        anchor = anchors.with_residual_tokens(anchor, tokenize(anchor.bare))
        outcome.anchor = anchor.to_public()

    # min_coverage 传 0：这一层的闸门由本函数自己施加，索引层不再重复过滤
    pool = max(limit * 3, limit)
    # 三个词项视图：o = 用户原话（兜底 + 一路召回），a = 自包含查询（排序），
    # e = 同义扩展词（把字面不接轨的片段捞回来）
    o_terms = tokenize(search_query)
    a_terms = tokenize(search_standalone)
    e_terms = tokenize(" ".join(expanded)) if expanded else []

    # **点名文件**：范围收在点名文件内，且不再过覆盖率闸门（理由见 `_fill_by_anchor`）。
    # 残余问法里一个主题词都没有时（"还有哪些规定"），按文件内顺序摆；
    # 有主题词时按相关度排。无论哪种，能不能答都由闸二（模型自评）判。
    if anchor is not None:
        _fill_by_anchor(outcome, index, anchor, limit, a_terms, e_terms, o_terms)
        return outcome

    path_a = index.search(search_standalone, top_k=pool, document_ids=scope, min_coverage=0.0)
    path_b = (
        index.search(" ".join(expanded), top_k=pool, document_ids=scope, min_coverage=0.0)
        if expanded else []
    )

    # **原话也要单独占一路**，而不是只用来算覆盖率。
    # 理由：候选池原先只由 A/E 两路供给，万一改写把问题带偏，
    # 原话本来能召回的片段**根本进不了池子**，覆盖率再宽松也救不回来。
    # 加这一路之后，R2 能召回的片段必定仍在池内——这是"改写只做加法"的第二半
    # （第一半是覆盖率取三视图最大值，见 `_score_combined`）。
    # 词项集合相同就不重复跑了：同一路跑两遍只会在 RRF 里把这一路偷偷加权。
    path_o: list[SearchHit] = []
    if set(o_terms) != set(a_terms):
        path_o = index.search(search_query, top_k=pool, document_ids=scope, min_coverage=0.0)

    fused_paths = [path_a]
    if path_b:
        fused_paths.append(path_b)
    if path_o:
        fused_paths.append(path_o)

    if len(fused_paths) > 1:
        fused = _rrf(fused_paths)
        hits = [
            hit for hit in (
                _score_combined(index, a_terms, e_terms, chunk_id, o_terms)
                for chunk_id in fused
            ) if hit is not None
        ][:pool]
        outcome.total_hits = len(fused)
    else:
        # 单路：沿用索引层已算好的覆盖率，一个数都不重算——
        # 保证"关掉改写"时行为与 R2 逐位相同，回滚不必改代码。
        hits = path_a
        outcome.total_hits = len(hits)

    outcome.recall_paths = {
        "original": len(path_o),
        "standalone": len(path_a),
        "expanded": len(path_b),
        "fused": outcome.total_hits,
        "rrf": len(fused_paths) > 1,
        # 三个视图各有多少个词项：评审问"改写到底改了什么"时看这三个数
        "terms": {"original": len(o_terms), "standalone": len(a_terms), "expanded": len(e_terms)},
    }
    # 零命中豁免只在单路时成立（R2 行为）：单路的 hits 全部来自 path_a，天然都有命中；
    # 融合模式下要逐条核"是不是三路都没直接命中"，见下面的 _path_uncovered
    recall_paths = fused_paths if len(fused_paths) > 1 else []
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
        # 融合模式下多一道：所有召回路径都零命中的片段，覆盖率恒为 0，不能靠豁免混进证据块
        _zero_hit = bool(recall_paths) and _path_uncovered(hit.chunk_id, recall_paths)
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
        if coverage >= threshold and not _zero_hit:
            passed.append(candidate)
        else:
            rejected.append(candidate)

    # 重新编 1..N：模型看到的编号必须连续，否则"编号越界"的判定会失去意义
    top = passed[:limit]
    # 邻片打分与主路径同口径（三个词项视图一起传），避免"补进来的证据换了把尺子"
    extras = _complete_neighbours(standalone, top, passed, index, threshold, outcome,
                                  a_terms=a_terms, e_terms=e_terms, o_terms=o_terms)
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
