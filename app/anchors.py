"""书名号锚定：用户**点名了哪份文件**，比"哪个词命中得多"更硬。

**它修的是什么（降级路径上真实可复现的错答案）**

前端「继续追问」的第一个建议是这么拼出来的（`AskView.vue` 的 `followUps`）：

    《本科生奖学金评审办法（2026年版）》里还有哪些与这个问题相关的规定？

这句话被送去检索时，书名号里的"2026"是一个**不可能命中的词**：
2026 版的正文里只写"本办法"，年份只存在于**文件名**里；反倒是 2025 版的
第一章正文里印着"（2025年版）"。于是实测（`.workbuddy/probe_degraded.py`）：

    候选只有 2 条，第 1 名是《…（2025年版）》第一章（0.769）
    2026 版自己的第二/三/四章全部 0.308 < 0.35，被闸门挡在门外

也就是——**用户点名了 2026 版，系统优先给他 2025 版**，还顺手把点名的那份
大部分章节扔掉了。这不是"检索得不够好"，是"检索把点名当成了主题词"。

**为什么不能只靠 R3 的查询改写**

改写确实能救：真实模型把这句话改写后，引用全部落在 2026 版（见链 P §P.5.2）。
但改写**没有模型就不存在**，而"无 API Key 时问答链路仍可用"是设计文档写死的降级策略
（§7.7）。降级下这条缺陷必然暴露，且暴露方式是最糟的一种：**给出一个版本错误的答案，
而不是拒答**。所以这一层必须是**确定性规则**，放在检索里、与改写解耦。

**规则（三道匹配，取最高的一档，不做并集）**

| 档 | 判据 | 例 |
| --- | --- | --- |
| 1 | 书名号内容与文件名**规范化后完全相同** | 《…（2026年版）》 → 只锚定 2026 版 |
| 2 | 去掉结尾括号后的**核心名相同** | 《本科生奖学金评审办法》 → 两版都锚定（版本冲突照旧提示） |
| 3 | 书名号内容（≥6 字）是文件名的**子串** | 《奖学金评审办法》 → 两版都锚定 |

取最高一档而不是并集：若取并集，《…（2026年版）》会先在第 1 档命中 2026 版，
又在第 3 档因"是 2025 版文件名（去掉年份后）的子串"把 2025 版也拉进来——
那就等于没锚定。

**锚定之后的两件事**

1. **书名号从检索文本里摘掉**。它是"指针"，不是"主题词"。留着它，年份/括号
   会进覆盖率分母，把点名的那份文件往下压；摘掉之后，剩下的才是真正要检索的问法。
2. **检索范围收窄到被点名的文件**（多个版本时是这几份）。用户问"《X》里…"，
   从《Y》里摘一段答他，即使字面更像，也是答错了文件。

**"开放式问法"为什么可以让闸门不适用**

`《X》里还有哪些与这个问题相关的规定？` 摘掉书名号后只剩「还有 / 问题 / 规定」——
没有主题词，任何片段都覆盖不了 0.35。可用户的意思很清楚：**把 X 里相关的规定列出来**，
X 本身就是主题。此时覆盖率闸门问的"这个主题库里有吗"已经由"X 在库里"回答了，
继续拿它挡人只会把可回答的问题判成"证据不足"（这正是用户报的那个症状）。

所以只在一个很窄的条件下让闸门不适用：**锚定命中 + 残余问法里没有任何主题词**。
残余里只要出现一个主题词（"转专业""金额""流程"…），就照常过闸——
问《图书馆办法》里怎么转专业，本来就该拒答。

这个判据**不改全局分词与停用词**：0.35 是 24 条评测集 5 点扫描定的（§7.2.1），
往分词器里加停用词会改变所有查询的分母，等于把定标好的阈值换了个含义。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field, replace
from typing import Iterable, Sequence

# 书名号内容的最大长度。政策文件名很少超过 40 字；放宽到 60 只为容纳
# "关于……的若干规定（试行）"这类长名，再长就多半不是文件名而是正文引用。
QUOTE_MAX_CHARS = 60

# 匹配档位：数字越小越硬。取**命中的最高档**，不并集（理由见模块文档）。
_TIER_EXACT = 1
_TIER_CORE = 2
_TIER_SUBSTRING = 3

# 核心名匹配的最短长度。低于 6 字的名（《办法》《规定》）会命中一切文件，
# 那不是锚定，是把检索范围缩成整个知识库。
MIN_FUZZY_CHARS = 6

_QUOTE_RE = re.compile(r"《([^》]{1,%d})》" % QUOTE_MAX_CHARS)

# 结尾括号段：（2026年版）、(试行)、【附件】都可能出现在文件名尾部。
# 只剥结尾、反复剥：「关于……的规定（试行）（修订）」要能一路剥到核心名。
_TRAILING_GROUP_RE = re.compile(r"\s*[（(【\[][^（）()【】\[\]]*[）)】\]]\s*$")

# **开放式问法的词表**。只用于判断"摘掉书名号后还有没有主题词"，
# 不参与分词、不参与覆盖率——它是这一层自己的判据，不该外溢。
#
# 收词标准：在政策问答里问"是什么/有哪些/怎么规定"的虚词与泛化词。
# 宁可少收："条件""材料""流程"这类**可能**是主题的词一律**不算**噪声，
# 因为一旦算错，闸门就会对"《X》的申请条件是什么"这类真问题失效。
_OPEN_WORDS = frozenset({
    "还有", "哪些", "哪个", "哪里", "什么", "怎么", "怎样", "如何", "为什么", "是否",
    "可以", "能否", "需要", "应该", "其他", "其它", "另外", "相关", "有关", "规定",
    "内容", "条款", "条文", "上面", "上述", "之前", "前面", "刚才", "这个", "那个",
    "这些", "那些", "一下", "具体", "详细", "介绍", "说明", "列举", "列出", "全部",
    "所有", "情况", "方面", "部分", "章节", "条", "款", "项", "里", "中", "该", "本",
    "它", "他", "她", "我们", "你", "我", "还", "又", "再", "都", "也", "和", "与",
    "及", "或", "对", "关于", "来说", "而言", "吗", "呢", "吧", "啊",
    # 指代词与"空壳名词"。「这个问题」里的"问题"分值上很像个主题词（库里的
    # "第七章 常见问题"真的含它），但它在这类追问里只承担指代，不承担主题。
    "问题", "疑问", "办法", "说法",
    # 追问里的"问多少/问时间"：这些是**问法**不是**主题**。
    # 少了它们，"那它要多久"会被判成"有主题词"，降级时的指代兜底就永远不触发
    # （R5 实测：加之前 `那它要多久` 的分词只剩 `多久`，被判成主题词）。
    "多久", "多长时间", "多长", "多少", "多少钱", "多少天", "几天", "几次",
    "几本", "几份", "几个", "几号", "什么时候", "要不要", "需不需要", "能不能",
})


@dataclass(frozen=True)
class DocumentAnchor:
    """一次锚定的结果。字段全部对外可见（会进节点轨迹）。"""

    # 命中的书名号原文（可能不止一个）
    quotes: tuple[str, ...]
    document_ids: tuple[str, ...]
    titles: tuple[str, ...]
    # 是否点到唯一一份文件。False 表示书名号对应同名的多个版本（或子串命中多份）
    pinned: bool
    # 检索用的问法：书名号**换成核心名**（去掉年份/版本括号）。
    # 为什么不整段删：书名号里往往就带着主题（"图书馆借阅与自习空间管理办法"），
    # 删干净会让"《X》对办理流程是怎么规定的"这种问法丢掉主语而误判为不相关。
    residual: str
    # 摘净版：书名号整段去掉，只剩"用户自己额外说的内容"。
    # 只用来判断"他是不是除了点名文件之外没提任何主题"（开放式问法）。
    bare: str
    # 残余问法里没有主题词 = 用户在说"把这份文件里的相关规定列出来"
    open_query: bool

    def to_public(self) -> dict:
        return {
            "quotes": list(self.quotes),
            "document_ids": list(self.document_ids),
            "titles": list(self.titles),
            "pinned": self.pinned,
            # 轨迹里只留摘净版：它最能说明"书名号之外用户还说了什么"。
            "residual": self.bare,
            "open_query": self.open_query,
        }


def _norm(text: str) -> str:
    """规范化：全角转半角（NFKC）、去空白、去书名号。

    NFKC 是关键：库里存的是「（2026年版）」，用户手打可能是半角「(2026年版)」，
    不做归一化就永远匹配不上，而"匹配不上"的表现是**静默退化成普通检索**。
    """
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("《", "").replace("》", "")
    return re.sub(r"\s+", "", text)


def _core(text: str) -> str:
    """去掉结尾括号段，得到"核心名"。反复剥，直到不再变化。"""
    name = _norm(text)
    while True:
        stripped = _TRAILING_GROUP_RE.sub("", name)
        if stripped == name or not stripped:
            return name
        name = stripped


def quotes_in(text: str) -> list[str]:
    """取出文本里所有书名号内容，保持出现顺序。"""
    return [m.group(1).strip() for m in _QUOTE_RE.finditer(text or "") if m.group(1).strip()]


def strip_quotes(text: str) -> str:
    """把书名号（含内容）整段摘掉，顺带把多余空白收成一个空格。

    用于判断"书名号之外用户还说了什么"。**不要拿它去做检索**：
    书名号里通常带着主题词，摘干净会把主语一起删掉（见 `residual_text`）。
    """
    return re.sub(r"\s{2,}", " ", _QUOTE_RE.sub(" ", text or "")).strip(" ，,。？?！!、；;：")


def residual_text(text: str) -> str:
    """检索用问法：书名号换成**核心名**（去掉结尾的年份/版本括号）。

    `《本科生奖学金评审办法（2026年版）》里还有哪些规定`
        → `本科生奖学金评审办法 里还有哪些规定`

    年份被去掉是关键：2026 版的正文里只写"本办法"，"2026"这个查询词
    一次也命中不了，却占着覆盖率的分母（实测把该文件的章节从 0.69 压到 0.308）。
    核心名留着，"奖学金/评审/办法"这些真主题词就还在。
    """
    def _replace(match: re.Match) -> str:
        return _core(match.group(1)) or " "
    return re.sub(r"\s{2,}", " ", _QUOTE_RE.sub(_replace, text or "")).strip()


def is_open_query(tokens: Iterable[str], noise: frozenset[str] = _OPEN_WORDS) -> bool:
    """这串词项里有没有主题词。空集合也算开放式（用户只说了文件名）。"""
    return not any(token not in noise for token in tokens)


def _match_tier(quote: str, title: str) -> int | None:
    """一条书名号 vs 一个文件名，命中的最高档；不命中返回 None。"""
    q, t = _norm(quote), _norm(title)
    if not q or not t:
        return None
    if q == t:
        return _TIER_EXACT
    qc, tc = _core(quote), _core(title)
    if qc and qc == tc and len(qc) >= MIN_FUZZY_CHARS:
        return _TIER_CORE
    if len(q) >= MIN_FUZZY_CHARS and q in t:
        return _TIER_SUBSTRING
    if qc and len(qc) >= MIN_FUZZY_CHARS and qc in tc:
        return _TIER_SUBSTRING
    return None


def resolve(query: str, documents: Sequence[tuple[str, str]],
            *, allowed: Iterable[str] | None = None) -> DocumentAnchor | None:
    """从问句里解析"点名的文件"。

    `documents` 是 `(document_id, title)` 列表（来自索引）。`allowed` 是调用方
    已经限定的文档范围（例如"只在知识库某几份里搜"）——两者取交集，
    交不出来就当作没锚定：**范围是调用方定的，不能被一句书名号突破**。
    """
    text = query or ""
    quotes = quotes_in(text)
    if not quotes or not documents:
        return None

    pool = [(str(doc_id), title or "") for doc_id, title in documents]
    if allowed is not None:
        allowed_set = {str(item) for item in allowed}
        pool = [(doc_id, title) for doc_id, title in pool if doc_id in allowed_set]
    if not pool:
        return None

    # 逐条书名号取"最高档"的命中；同一条书名号不跨档并集（理由见模块文档）
    matched: list[tuple[str, str]] = []
    hit_quotes: list[str] = []
    for quote in quotes:
        best_tier: int | None = None
        best: list[tuple[str, str]] = []
        for doc_id, title in pool:
            tier = _match_tier(quote, title)
            if tier is None:
                continue
            if best_tier is None or tier < best_tier:
                best_tier, best = tier, [(doc_id, title)]
            elif tier == best_tier:
                best.append((doc_id, title))
        if best:
            hit_quotes.append(quote)
            for item in best:
                if item not in matched:
                    matched.append(item)

    if not matched:
        return None

    bare = strip_quotes(text)
    return DocumentAnchor(
        quotes=tuple(hit_quotes),
        document_ids=tuple(doc_id for doc_id, _ in matched),
        titles=tuple(title for _, title in matched),
        pinned=len(matched) == 1,
        residual=residual_text(text),
        bare=bare,
        # open_query 的判定要把"摘净版"分词后才能做，而分词属于索引层。
        # 这里先给一个保守默认（摘净后为空才算开放），真正的判定在 retrieval 里
        # 用 `with_residual_tokens` 补上——避免 anchors 反向依赖 indexing。
        open_query=not bare,
    )


def with_residual_tokens(anchor: DocumentAnchor, tokens: Sequence[str]) -> DocumentAnchor:
    """用**摘净版**问法的分词结果补全 `open_query`。

    单独一步是为了让本模块**不 import 索引层**：分词口径只有一份（`indexing.tokenize`），
    这里只接收它的输出。摘净后为空时保持 `open_query=True`。

    注意传进来的必须是 `tokenize(anchor.bare)` 而不是 `tokenize(anchor.residual)`：
    residual 里带着书名号的核心名，那份文件自己的名字当然"覆盖"得了自己，
    拿它判开放式问法会永远判成"有主题"。
    """
    return replace(anchor, open_query=not tokens or is_open_query(tokens))
