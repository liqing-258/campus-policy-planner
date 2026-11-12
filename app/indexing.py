"""BM25 索引（M3 建立，M4 供检索使用）。

为什么自研而不是用 rank_bm25 / Elasticsearch：
- 只有 1 万级片段，全量打分 < 10ms，引入外部依赖（含 Docker）不值得；
- 需要把"章节标题命中"计入打分，第三方实现的接口不一定留这个口子；
- 20 行核心公式，可解释、可在答辩时逐行讲清。

索引存在内存里，进程启动后首次查询时构建。**不落盘**：
片段正文本身在 SQLite，重建索引只花几十毫秒，落盘反而引入"索引与正文不一致"的坑。
"""

from __future__ import annotations

import math
import threading
import time
from collections import Counter
from dataclasses import dataclass, field

# BM25 参数（业界常用默认值，未调参——调参需要评测集，M4 再做）
K1 = 1.5
B = 0.75
# 章节标题命中加权：标题里的词对"这条政策讲什么"的判断力更强
SECTION_TITLE_BOOST = 1.5

_STOPWORDS = frozenset(
    "的 了 和 与 及 或 在 是 为 对 从 到 按 由 之 其 该 本 等 中 上 下 有 无 不 也 就 都 而 并 或 一个 进行 相关".split()
)
# 疑问/指代类虚词：用户提问里高频出现但不携带主题信息，留着会虚增覆盖率与分数
_INTERROGATIVE_STOPWORDS = frozenset(
    "怎么 如何 什么 哪些 哪个 哪儿 多少 是否 请问 一下 有没有 以及 这个 那个 我们".split()
)
_STOPWORDS = _STOPWORDS | _INTERROGATIVE_STOPWORDS

_jieba_lock = threading.Lock()
_jieba_ready = False

# 是否在词项之外追加"汉字二元组"（bigram）。见 tokenize() 的说明，默认开启。
USE_BIGRAMS = True

_CJK_RANGE = ("\u4e00", "\u9fff")


def _is_cjk(char: str) -> bool:
    return _CJK_RANGE[0] <= char <= _CJK_RANGE[1]


def _bigrams(word: str) -> list[str]:
    """纯中文词切成长度 2 的滑动窗口。非中文词（英文、数字、混合）不切。

    两字词的 bigram 就是它自己（"申请" → "申请"），必须排除，
    否则同一个词项被计两次 term_freq，BM25 分数凭空抬高、覆盖率分母也被撑大。
    """
    if len(word) < 2 or not all(_is_cjk(ch) for ch in word):
        return []
    return [word[i:i + 2] for i in range(len(word) - 1) if word[i:i + 2] != word]


def tokenize(text: str, *, bigrams: bool | None = None) -> list[str]:
    """中文 jieba + 英文/数字小写切分。返回有意义的词项列表（含重复）。

    **为什么还要额外产出 bigram**——M4 实测发现的真缺陷，不是画蛇添足：

    jieba 对同一个词在不同上下文里切法不一致。实测：

        片段「上一学年成绩单」 → ['学年成绩']        ← 没有切出"成绩单"
        查询「那成绩单要原件吗」 → ['成绩单', '原件']

    两边字面交集为空（"成绩单" ≠ "学年成绩"），BM25 直接判定零命中，
    用户问"成绩单要什么格式"会得到"未找到足够依据"——而原文白纸黑字写着。
    加自定义词典也治不好：实测 `jieba.add_word("成绩单")` 之后该句仍切出"学年成绩"。

    这是中文检索的固有难点：分词器的目标是"最优切分"，而检索要的是"切法一致"。
    解法是同时保留**字符二元组**：片段产出「学年/年成/成绩」，查询产出「成绩/绩单」，
    交集"成绩"即可召回。代价是词项数增加约一倍，但 IDF 会自动压低高频 bigram 的权重，
    排序质量实测反而更好（见 docs/90-AI协作留痕/06-M4问答可信.md）。

    `bigrams=False` 保留原行为，用于对比实验与回归定位。
    """
    global _jieba_ready
    if not text:
        return []
    import jieba

    if not _jieba_ready:
        with _jieba_lock:
            if not _jieba_ready:
                jieba.setLogLevel(60)  # 关掉 "Building prefix dict" 之类噪音
                jieba.initialize()
                _jieba_ready = True

    want_bigrams = USE_BIGRAMS if bigrams is None else bigrams
    tokens: list[str] = []
    for token in jieba.cut(text):
        item = token.strip().lower()
        if not item or item in _STOPWORDS:
            continue
        if len(item) == 1 and not item.isascii():
            # 单字中文噪声大（"的""在"已被停用词拦掉，剩下多为虚词）
            continue
        if item.isspace():
            continue
        tokens.append(item)
        if want_bigrams:
            tokens.extend(_bigrams(item))
    return tokens


@dataclass
class IndexedChunk:
    chunk_id: str
    document_id: str
    document_title: str
    content: str
    page_number: int | None
    section_title: str | None
    # 片段在文件内的序号。只用于"补相邻片段"——政策文件的上下文常跨段，
    # 但命中段落单独看又不够，需要知道谁挨着谁。
    chunk_index: int = 0
    term_freq: Counter = field(default_factory=Counter)
    length: int = 0


@dataclass
class SearchHit:
    chunk_id: str
    document_id: str
    document_title: str
    content: str
    page_number: int | None
    section_title: str | None
    score: float
    # 命中查询词项的比例（0~1）。M4 的拒答判断主要依赖它，而不是 score——理由见 search() 文档串。
    coverage: float = 0.0
    matched_terms: tuple[str, ...] = ()
    chunk_index: int = 0


class Bm25Index:
    """可变索引。构建后只读；文档增删后调用 invalidate() 重建。"""

    def __init__(self) -> None:
        self._chunks: list[IndexedChunk] = []
        self._df: Counter = Counter()
        self._by_id: dict[str, IndexedChunk] = {}
        self._avg_len = 0.0
        self._built_at: float | None = None
        self._build_ms = 0.0
        self._lock = threading.RLock()

    # ---------- 构建 ----------
    def build(self, rows: list[dict]) -> "Bm25Index":
        """rows 需含 chunk_id/document_id/document_title/content/page_number/section_title。"""
        started = time.perf_counter()
        chunks: list[IndexedChunk] = []
        df: Counter = Counter()
        total_len = 0
        # 调用方没给 chunk_index 时按文件内出现顺序补一个。
        # 不能让所有人默认 0：那样 `neighbours()` 会把整份文件都当成"紧邻"。
        fallback_index: Counter = Counter()

        for row in rows:
            tokens = tokenize(row["content"])
            title_tokens = tokenize(row.get("section_title") or "")
            freq: Counter = Counter(tokens)
            for token in title_tokens:
                freq[token] += SECTION_TITLE_BOOST
            if not freq:
                continue
            document_id = row["document_id"]
            position = row.get("chunk_index")
            if position is None:
                position = fallback_index[document_id]
            fallback_index[document_id] = max(fallback_index[document_id], int(position) + 1)
            chunks.append(IndexedChunk(
                chunk_id=row["chunk_id"],
                document_id=document_id,
                document_title=row.get("document_title") or "",
                content=row["content"],
                page_number=row.get("page_number"),
                section_title=row.get("section_title"),
                chunk_index=int(position),
                term_freq=freq,
                length=len(tokens),
            ))
            total_len += len(tokens)
            for term in set(tokens) | set(title_tokens):
                df[term] += 1

        with self._lock:
            self._chunks = chunks
            self._df = df
            self._by_id = {chunk.chunk_id: chunk for chunk in chunks}
            self._avg_len = (total_len / len(chunks)) if chunks else 0.0
            self._built_at = time.time()
            self._build_ms = (time.perf_counter() - started) * 1000
        return self

    def invalidate(self) -> None:
        with self._lock:
            self._chunks = []
            self._df = Counter()
            self._by_id = {}
            self._avg_len = 0.0
            self._built_at = None

    # ---------- 查询 ----------
    @property
    def is_built(self) -> bool:
        return self._built_at is not None

    @property
    def size(self) -> int:
        return len(self._chunks)

    def stats(self) -> dict:
        return {
            "chunks": len(self._chunks),
            "terms": len(self._df),
            "avg_chunk_terms": round(self._avg_len, 1),
            "build_ms": round(self._build_ms, 1),
            "built_at": self._built_at,
        }

    def search(self, query: str, *, top_k: int = 6,
               document_ids: list[str] | None = None,
               min_score: float = 0.0,
               min_coverage: float = 0.0) -> list[SearchHit]:
        """返回按 BM25 分数降序的片段。空查询或无命中返回空列表。

        **关于"能不能靠分数判断相关性"——实测结论是不能。**
        在 3 片段样例上实测（见 tests/test_indexing.py）：

            国家励志奖学金 申请条件（相关）   top1 = 3.4398
            转专业学分认定流程（无关）        top1 = 1.7997   ← 无关但很高
            保研推免条件（无关）              top1 = 1.4509
            申请要交哪些材料（相关）          top1 = 1.3687   ← 相关却更低

        原因：BM25 之和取决于"命中了几个词"与"这些词的 idf"，只要有一个罕见词命中就能把分数顶起来，
        与"这条片段是否真的在回答这个问题"无关。所以**别指望一个绝对分数阈值能筛掉无关查询**。

        因此这里额外输出 `coverage`＝命中的查询词项占比，并把两道闸门做成可选参数（默认 0 = 不过滤，
        保持"检索永远给出候选"的原始语义）。M4 的拒答用 coverage 做前置粗筛，再由 LLM 判定证据是否充分。
        """
        terms = tokenize(query)
        if not terms or not self._chunks:
            return []

        unique_terms, idf = self._query_stats(terms)
        allowed = set(document_ids) if document_ids else None
        hits: list[SearchHit] = []
        for chunk in self._chunks:
            if allowed and chunk.document_id not in allowed:
                continue
            score, coverage, matched = self._score(chunk, unique_terms, idf)
            if score <= 0:
                continue
            if score < min_score or coverage < min_coverage:
                continue
            hits.append(SearchHit(
                chunk_id=chunk.chunk_id,
                document_id=chunk.document_id,
                document_title=chunk.document_title,
                content=chunk.content,
                page_number=chunk.page_number,
                section_title=chunk.section_title,
                score=round(score, 4),
                coverage=round(coverage, 4),
                matched_terms=tuple(matched),
                chunk_index=chunk.chunk_index,
            ))

        # 先按覆盖率、再按分数：覆盖更全的片段通常更贴合问题整体，而不只是撞上一个罕见词
        hits.sort(key=lambda h: (h.coverage, h.score), reverse=True)
        return hits[:top_k]

    # ---------- 单片段打分与邻片定位 ----------
    def _query_stats(self, terms: list[str]) -> tuple[list[str], dict[str, float]]:
        """查询词去重 + idf。覆盖率的分母是去重后的词项数。"""
        unique_terms = list(dict.fromkeys(terms))
        total = len(self._chunks)
        idf = {}
        for term in unique_terms:
            df = self._df.get(term, 0)
            # 加一平滑，避免 df=0 时除零与负分
            idf[term] = math.log(1 + (total - df + 0.5) / (df + 0.5))
        return unique_terms, idf

    def _score(self, chunk: IndexedChunk, unique_terms: list[str],
               idf: dict[str, float]) -> tuple[float, float, list[str]]:
        score = 0.0
        matched: list[str] = []
        norm = K1 * (1 - B + B * chunk.length / (self._avg_len or 1))
        for term in unique_terms:
            freq = chunk.term_freq.get(term, 0)
            if not freq:
                continue
            matched.append(term)
            score += idf[term] * freq * (K1 + 1) / (freq + norm)
        coverage = len(matched) / len(unique_terms) if unique_terms else 0.0
        return score, coverage, matched

    def score_chunk(self, query: str, chunk_id: str) -> SearchHit | None:
        """按与 `search()` 完全相同的口径，给单个片段打分。

        为什么要公开这一步：补相邻片段时不能拍一个分数上去，
        否则"这条邻片够不够格"就变成了另一套标准，覆盖率闸门形同虚设。
        """
        with self._lock:
            chunk = self._by_id.get(chunk_id)
            if chunk is None:
                return None
            unique_terms, idf = self._query_stats(tokenize(query))
            if not unique_terms:
                return None
            score, coverage, matched = self._score(chunk, unique_terms, idf)
            return SearchHit(
                chunk_id=chunk.chunk_id,
                document_id=chunk.document_id,
                document_title=chunk.document_title,
                content=chunk.content,
                page_number=chunk.page_number,
                section_title=chunk.section_title,
                score=round(score, 4),
                coverage=round(coverage, 4),
                matched_terms=tuple(matched),
                chunk_index=chunk.chunk_index,
            )

    def neighbours(self, document_id: str, chunk_index: int, span: int = 1) -> list[IndexedChunk]:
        """同一文件里紧挨着的片段（前后各 span 个），按序号排序。"""
        with self._lock:
            return sorted(
                (
                    c for c in self._chunks
                    if c.document_id == document_id
                    and abs(c.chunk_index - chunk_index) <= span
                    and c.chunk_index != chunk_index
                ),
                key=lambda c: c.chunk_index,
            )


# 进程级索引实例：脚本（无 app 上下文）与默认回退使用
index = Bm25Index()

# 每个 app 实例一个索引（存在 app.extensions 里），避免测试间互相污染
EXTENSION_KEY = "bm25_index"


def get_index() -> Bm25Index:
    """取当前 app 的索引实例；不在应用上下文时退回进程级单例（供脚本使用）。"""
    from flask import current_app, has_app_context

    if has_app_context():
        instance = current_app.extensions.get(EXTENSION_KEY)
        if isinstance(instance, Bm25Index):
            return instance
    return index


def rebuild_index() -> dict:
    """从当前会话重建索引（需在应用上下文内调用）。返回统计信息。"""
    from app.models import Document, DocumentChunk, db

    rows = db.session.execute(
        db.select(
            DocumentChunk.id, DocumentChunk.document_id, DocumentChunk.content,
            DocumentChunk.page_number, DocumentChunk.section_title, Document.title,
            DocumentChunk.chunk_index,
        )
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(Document.deleted_at.is_(None))
        .order_by(DocumentChunk.document_id, DocumentChunk.chunk_index)
    ).all()

    payload = [
        {
            "chunk_id": row[0],
            "document_id": row[1],
            "content": row[2],
            "page_number": row[3],
            "section_title": row[4],
            "document_title": row[5],
            "chunk_index": row[6],
        }
        for row in rows
    ]
    target = get_index()
    target.build(payload)
    return target.stats()


def build_from_database(app) -> dict:
    """脚本入口：在给定 app 上重建索引。"""
    with app.app_context():
        return rebuild_index()


# 本进程是否已经"尝试过懒加载"。有了它，"库确实是空的"这种情况不会每次查询都扫一遍表。
LOADED_KEY = "bm25_index_loaded"


def ensure_index_loaded() -> bool:
    """进程启动后首次检索时，把库里的片段装进内存索引。返回本次是否真的重建了。

    为什么必须有这一步：索引不落盘，重启后内存里是空的，而库里的片段还在。
    缺了它，"问一个库里明明有答案的问题"会被答成"知识库为空：尚未上传任何已解析的政策文件"
    ——M4 联调时真踩到了（`/health` 报 document_count=4、chunk_count=10，
    `/qa/ask` 却说知识库为空），而且这个错误信息会把排查方向指向"去重新上传文件"。

    只在索引为空时才扫表；上传/重解析/删除都会主动 rebuild，所以不会读到过期数据。
    """
    from flask import current_app, has_app_context

    current = get_index()
    if current.is_built and current.size:
        return False  # 已经有数据了，不必再查库
    if has_app_context():
        if current_app.extensions.get(LOADED_KEY):
            return False  # 本进程已尝试过，且当时库就是空的
        current_app.extensions[LOADED_KEY] = True

    from sqlalchemy import func, select

    from app.models import DocumentChunk, db

    total = db.session.execute(select(func.count()).select_from(DocumentChunk)).scalar() or 0
    if not total:
        return False
    rebuild_index()
    return True
