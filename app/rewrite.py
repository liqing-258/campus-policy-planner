"""查询改写（R3）：把"人话追问"改写成"检索得动的话"。

**它解决的两个真缺陷，不是理论优化**

1. 追问。点「继续追问」时前端会拼出"《X》里还有哪些与**这个问题**相关的规定？"，
   其中"这个问题"在检索里是纯噪声：它占着一个查询词位（"问题"不在停用词表里），
   却在任何政策片段里都命不中，于是覆盖率被稀释，好端端的追问被判成"主题不相关"。
   带会话历史时更糟——用户说"那它要多久"，字面上一个主题词都没有。

2. 同义说法。政策原文写"评审办法""申请材料"，学生说"拿奖学金要准备啥"。
   BM25 靠字面命中，字面不通就零召回，而这不是知识库缺内容，是**问法与写法不接轨**。

**为什么放在检索之前、而不是塞进 intent 节点**

`app/intent.py` 是刻意的纯规则模块（其模块文档写了三条理由），
它的输出只影响"要不要建议去做资格匹配"，不参与召回。
把一次模型调用塞进去，等于让一个不参与决策的节点开始产生网络延迟，
而且会让"意图识别 2.3 秒"这种耗时无法解释。改写在语义上属于检索的前置步骤，
所以由 `app/qa.py` 在 retrieval 节点之前调用，结果记进 **retrieval 节点**的 detail。

**降级是这个模块的第一职责，不是附带**

改写失败绝不能把问答链路带崩。四种情况一律回退原查询：
未配置模型、超时、上游报错、输出不是约定的 JSON 形状。
回退时 `applied=False` 且 `reason` 写明原因——**"没改写"与"改写成了空"必须能区分**，
否则线上看到的就是"检索效果莫名其妙变差"，而日志里什么都没有。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app import llm
from app.errors import ApiError
from app.runtime import current_settings

# **历史口径的唯一来源**：3 轮、单条 240 字。
# `app/qa.py` 直接引用这几个常量与 `format_history`，不再自己复制一份——
# 两条链路读同一张表，若各写一份，改了一处忘了另一处，
# 同一句追问在"改写"与"生成"两步看到的"历史"就不一样了。
HISTORY_PAIRS = 3
HISTORY_MSG_MAX_CHARS = 240

# Prompt 逐字来自需求书，不做"优化"：这是一份可复核的契约。
# 唯一改动是把 {history}/{query} 换成 str.format 的占位符写法。
SYSTEM_PROMPT = """你是校园政策知识库的查询改写专家。给定对话历史和当前问题，请：
把当前问题改写成一个不依赖对话历史的、自包含的检索查询。如果问题中出现“这个问题”“它”“上面说的”等指代词，必须从历史中还原完整实体。
输出 3~5 个适合政策知识库检索的关键词或短语，优先使用政策文件可能采用的正式表述或同义表述（如“国家奖学金”可扩展为“励志奖学金”、“评审办法”）。
只输出 JSON 格式，不要输出任何解释：{"standalone_query": "...", "expanded_terms": ["...", "..."]}"""

USER_TEMPLATE = """对话历史：{history}
当前问题：{query}"""

# 没有历史时的占位。写"（无）"而不是留空：模型看到空行容易把整段格式理解错，
# 而"（无）"明确告诉它"这次没有上文可还原"。
NO_HISTORY = "（无，这是本轮的第一个问题）"


@dataclass
class RewriteResult:
    """一次改写的结果。字段全部对外可见（会进节点轨迹）。"""

    standalone_query: str
    expanded_terms: list[str] = field(default_factory=list)
    # 是否真的用上了模型改写。False 时 standalone_query 就是原问题。
    applied: bool = False
    # 是否**发起过**模型调用（失败的调用也消耗了时间，轨迹里不能当没发生）
    attempted: bool = False
    # 没生效的原因。**正常跳过也要有原因**，否则排查看不出"为什么没改写"。
    reason: str | None = None
    model: str | None = None
    latency_ms: int | None = None

    @property
    def query(self) -> str:
        """第一路检索用的查询。"""
        return self.standalone_query or ""

    def to_public(self) -> dict:
        return {
            "standalone_query": self.standalone_query,
            "expanded_terms": list(self.expanded_terms),
            "applied": self.applied,
            "attempted": self.attempted,
            "reason": self.reason,
            "model": self.model,
            "latency_ms": self.latency_ms,
        }


def format_history(history: list[dict]) -> str:
    """把消息列表压成"用户：… / 助手：…"的纯文本。

    单条截断 240 字：助手回答动辄三四百字，原样拼进去会让"当前问题"在
    Prompt 里的位置被挤到很后面，而这次调用真正要处理的是当前问题。

    这个函数被 `app/qa.py` 的生成 Prompt 一起复用（`qa.format_history` 就是它），
    所以两处的历史写法必然一致——**不要**在这里为了改写的口味单独加前缀。
    """
    lines = []
    for item in history:
        role = "用户" if item.get("role") == "user" else "助手"
        text = (item.get("content") or "").strip().replace("\n", " ")
        if len(text) > HISTORY_MSG_MAX_CHARS:
            text = text[:HISTORY_MSG_MAX_CHARS] + "…"
        if text:
            lines.append(f"{role}：{text}")
    return "\n".join(lines) if lines else NO_HISTORY


def _clean_terms(raw, query: str, limit: int) -> list[str]:
    """清洗扩展词：去空、去重、去与原问题字面相同的词、截断到上限。

    为什么要去掉与原问题相同的词：它已经在第一路里了，重复放进第二路
    会让同一批字面词在 RRF 里被计两次，等于偷偷给一路加权。
    """
    if not isinstance(raw, (list, tuple)):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        term = item.strip()
        if not term or term in seen:
            continue
        if term == (query or "").strip():
            continue
        seen.add(term)
        out.append(term)
        if len(out) >= limit:
            break
    return out


def rewrite_query(query: str, history: list[dict] | None = None) -> RewriteResult:
    """改写问题。**任何失败都退回原查询**，不向上抛。"""
    original = (query or "").strip()
    settings = current_settings()

    if not settings.query_rewrite_enabled:
        return RewriteResult(original, applied=False, reason="改写已关闭（QUERY_REWRITE_ENABLED=0）")
    if not llm.is_configured():
        # 降级：没有 Key 时链路必须照常可用——这正是设计要求的"无模型也能检索出原文"。
        return RewriteResult(original, applied=False, reason="未配置模型，按降级策略使用原查询")
    if not original:
        return RewriteResult(original, applied=False, reason="空问题，无需改写")

    user_prompt = USER_TEMPLATE.format(
        history=format_history(history or []), query=original
    )
    try:
        parsed, result = llm.chat_json(
            SYSTEM_PROMPT, user_prompt,
            temperature=0.1, max_tokens=settings.query_rewrite_max_tokens,
        )
    except ApiError as exc:
        # 超时 / 上游 5xx / 输出非法：这三类都只影响"这次改写没生效"，
        # 不该让用户看到一个报错页——检索用原话照样能跑。
        return RewriteResult(original, applied=False, attempted=True,
                             reason=f"改写失败，已回退原查询：{exc.message}")
    except Exception as exc:  # pragma: no cover - 兜底，避免改写把整条链路带崩
        return RewriteResult(original, applied=False, attempted=True,
                             reason=f"改写异常，已回退原查询：{type(exc).__name__}")

    standalone = parsed.get("standalone_query")
    if not isinstance(standalone, str) or not standalone.strip():
        # 形状不对就整条弃用，不做"凑合取一半"：半个改写结果比不改写更难排查。
        return RewriteResult(
            original, applied=False, attempted=True,
            reason="模型输出缺少 standalone_query，已回退原查询",
            model=result.model, latency_ms=result.latency_ms,
        )

    terms = _clean_terms(parsed.get("expanded_terms"), original,
                         max(1, settings.query_rewrite_max_terms))
    return RewriteResult(
        standalone.strip(),
        expanded_terms=terms,
        applied=True,
        attempted=True,
        # 没给扩展词也算改写成功（standalone 可能已经把指代还原了），但要能看出来
        reason=None if terms else "模型未给出扩展词，仅使用自包含查询",
        model=result.model,
        latency_ms=result.latency_ms,
    )
