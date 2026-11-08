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


def tokenize(text: str) -> list[str]:
    """中文 jieba + 英文/数字小写切分。返回有意义的词项列表（含重复）。"""
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
    return tokens


@dataclass
class IndexedChunk:
    chunk_id: str
    document_id: str
    document_title: str
    content: str
    page_number: int | None
    section_title: str | None
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


class Bm25Index:
    """可变索引。构建后只读；文档增删后调用 invalidate() 重建。"""

    def __init__(self) -> None:
        self._chunks: list[IndexedChunk] = []
        self._df: Counter = Counter()
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

        for row in rows:
            tokens = tokenize(row["content"])
            title_tokens = tokenize(row.get("section_title") or "")
            freq: Counter = Counter(tokens)
            for token in title_tokens:
                freq[token] += SECTION_TITLE_BOOST
            if not freq:
                continue
            chunks.append(IndexedChunk(
                chunk_id=row["chunk_id"],
                document_id=row["document_id"],
                document_title=row.get("document_title") or "",
                content=row["content"],
                page_number=row.get("page_number"),
                section_title=row.get("section_title"),
                term_freq=freq,
                length=len(tokens),
            ))
            total_len += len(tokens)
            for term in set(tokens) | set(title_tokens):
                df[term] += 1

        with self._lock:
            self._chunks = chunks
            self._df = df
            self._avg_len = (total_len / len(chunks)) if chunks else 0.0
            self._built_at = time.time()
            self._build_ms = (time.perf_counter() - started) * 1000
        return self

    def invalidate(self) -> None:
        with self._lock:
            self._chunks = []
            self._df = Counter()
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

        unique_terms = list(dict.fromkeys(terms))  # 去重且保序，覆盖率分母用它
        total = len(self._chunks)
        idf = {}
        for term in unique_terms:
            df = self._df.get(term, 0)
            # 加一平滑，避免 df=0 时除零与负分
            idf[term] = math.log(1 + (total - df + 0.5) / (df + 0.5))

        allowed = set(document_ids) if document_ids else None
        hits: list[SearchHit] = []
        for chunk in self._chunks:
            if allowed and chunk.document_id not in allowed:
                continue
            score = 0.0
            matched: list[str] = []
            norm = K1 * (1 - B + B * chunk.length / (self._avg_len or 1))
            for term in unique_terms:
                freq = chunk.term_freq.get(term, 0)
                if not freq:
                    continue
                matched.append(term)
                score += idf[term] * freq * (K1 + 1) / (freq + norm)
            if score <= 0:
                continue
            coverage = len(matched) / len(unique_terms)
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
            ))

        # 先按覆盖率、再按分数：覆盖更全的片段通常更贴合问题整体，而不只是撞上一个罕见词
        hits.sort(key=lambda h: (h.coverage, h.score), reverse=True)
        return hits[:top_k]


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
