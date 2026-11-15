"""智能政策推荐（R13）。

## 这一层没有模型、没有新表、没有第二套资格判断

需求里最容易做错的一件事，是把"推荐"理解成"让模型看画像猜奖项"。这里一条
模型调用都没有，理由和 `matching.py` 顶部写的是同一条：**推荐结果会直接影响
一个学生去不去申请奖助学金**，它必须可复现、可解释、可断言。

因此四个输入全部来自已有的表，一个都不新造：

| 输入 | 来源 | 为什么是它 |
| --- | --- | --- |
| 学生画像 | `match_sessions.profile_json`（该用户最近一次匹配） | 画像本来就只在"提交匹配"时落库；`analytics.py` 的管理看板也读这张表，两处口径天然一致 |
| 资格条件 | `policy_rules`（**只认 `review_status=approved`**） | 未复核的规则不得作为确定性依据（需求 4.2） |
| 政策原文 | `document_chunks` | 卡片上的"主要申请条件"和"原文片段"逐字来自这里，编目只存指向 |
| 文件与版本 | `documents` | 版本选择复用 `guides._pick_document`：active 优先，2026 版自动压过 2025 版 |

输出同样不落库。它不是"计算结果没保存"，而是**这个结果本来就是纯函数**：
同一个画像 + 同一批规则，任意时刻重算都得到同一份推荐，刷新页面自然一致。
反过来，如果为推荐单开一张快照表，规则一复核、政策一换版，表里的推荐就与
真相脱节了——**过期的推荐比没有推荐更危险**。

## 三态用的是同一套语义

需求 3 的三态与 `matching` 的三态是同一件事，只是换了面向学生的说法：

| 本模块 | `matching` | 判据 |
| --- | --- | --- |
| 可能符合 | `met` | 有已复核条件，且全部满足 |
| 暂不符合 | `not_met` | **至少一条**有明确依据的不符合（哪怕还有别的条件缺信息） |
| 暂无法判断 | `unknown` | 缺字段 / 条款主观 / 规则未复核 / 根本没有规则 |

聚合顺序是 `not_met > unknown > met`，与需求 3.2 的措辞一致："至少存在一项
有明确依据的不符合条件"就足以判成暂不符合。

## "编目"这件事仍然是人工的

`SUBJECTS` 是人工编目的：它回答"库里哪些政策算得上一个可推荐的事项，指向哪份
文件、哪个奖项标签、哪一条原文"。这和 R12 的 `GUIDE_ITEMS` 是同一个取舍——
自动抽取 + 自动归类在"哪一章是申请条件""这份文件算不算奖学金"上错得比人快，
而错一次的代价是学生按错的清单去准备材料（已知限制 C-19）。

编目**不存正文、不存判断**：`audience` 只是把"原文写明的适用对象"翻译成画像
字段（例如「家庭经济困难的全日制本专科学生」→ `poverty_certified = yes`），
比较仍然交给 `matching.evaluate()`；原文引用逐字取自 chunk。所以政策换版时，
卡片跟着换，编目不用动。

## 覆盖不到的地方要写在明面上

需求要求优先覆盖"国家奖学金 / 国家励志奖学金 / 校级奖学金 / 助学金 / 优秀学生
评选"。当前知识库的真实情况是：

- 国家奖学金：**只有兼得限制，没有评审条件** → 卡片给「暂无法判断」并说明；
- 校级优秀学生奖学金：原文有条件，但 3 条规则**被退回复核** → 不做资格判定，
  只把原文条件摆出来；
- 国家助学金：**库里没有这份文件** → 进 `coverage.missing`，不生成卡片。

这三种"没有"各不相同，混成一句"暂无数据"就没人能判断是系统坏了还是库空了。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from flask import Blueprint

from app.auth import current_user, login_required
from app.contract import ok
from app.guides import (
    _clean,
    _doc_meta,
    _find_chunk,
    _load_context,
    _pick_document,
    _slice_article,
)
from app.matching import FIELD_LABELS, OP_TEXT, evaluate
from app.models import MatchSession, PolicyRule, db, utcnow
from shared.enums import (
    CONDITION_RULE_TYPES,
    EFFECTIVE_ACTIVE,
    MATCH_MET,
    MATCH_NOT_MET,
    MATCH_UNKNOWN,
    OP_CONTAINS_ANY,
    OP_EQUALS,
    OP_GTE,
    PROFILE_FIELDS,
    REVIEW_APPROVED,
    REVIEW_NEEDS,
    REVIEW_REJECTED,
    RULE_DEADLINE,
    RULE_MATERIAL,
    RULE_PROCESS,
    UNKNOWN_MISSING_INFO,
)

bp = Blueprint("recommend", __name__)

# 卡片上的分类。与指南的分类不同：这里按"推荐语境的远近"排，
# 奖学金/评优在前，资助与服务在后（资助类多数是"暂无法判断"，排序上让位）。
CATEGORY_LABEL = {
    "scholarship": "奖学金",
    "honors": "评优",
    "support": "资助与服务",
    "service": "校园事务",
}

STATUS_LABEL = {
    MATCH_MET: "可能符合",
    MATCH_NOT_MET: "暂不符合",
    MATCH_UNKNOWN: "暂无法判断",
}

CONDITION_LABEL = {
    MATCH_MET: "符合",
    MATCH_NOT_MET: "不符合",
    MATCH_UNKNOWN: "无法判定",
}

# 画像枚举值的中文。只用于**推荐理由和未推荐原因**这类给人看的句子：
# 「家庭经济困难认定等于 yes」在这里读起来像报错信息。
VALUE_LABEL = {
    "yes": "已认定",
    "no": "未认定",
    "none": "无",
    "any": "有",
    "enrolled": "在籍在读",
    "suspended": "休学",
    "graduated": "已毕业",
}

# 排序权重。**状态是第一位的**：学生先看"可能符合"，再考虑"还不知道"，
# 最后才是"已经明确不符合的"（后者仍有价值——它说清了差在哪一条）。
# 其余权重都是同一状态内的次序：已复核条件命中越多越靠前；画像能确认属于
# 适用对象的靠前；现行有效优先于被替代版本。
STATUS_SCORE = {MATCH_MET: 300, MATCH_UNKNOWN: 200, MATCH_NOT_MET: 100}
CATEGORY_SCORE = {"scholarship": 12, "honors": 10, "support": 8, "service": 4}
# 前置条件缺失的加一点权重："先把困难认定办了"通常是这一年真正的第一步。
PREREQ_BONUS = 30

DISCLAIMER = (
    "推荐依据你最近一次资格匹配提交的画像、已复核的政策规则与政策原文自动生成，"
    "仅用于提示可能适合的政策，不构成资格认定；未通过复核的规则不参与判定，"
    "最终结果以学校审核为准。"
)


# ---------------------------------------------------------------- 编目
def _subject(
    key: str,
    title: str,
    category: str,
    kind: str,
    doc_key: str,
    *,
    target_group: str | None = None,
    guide_key: str | None = None,
    audience: tuple = (),
    audience_quote: dict | None = None,
    condition_quote: dict | None = None,
    evidence_quote: dict | None = None,
    conditions_from_quote: bool = True,
    prereq_for: tuple = (),
    note: str | None = None,
    ask: str | None = None,
) -> dict:
    """一条可推荐的候选政策。

    `target_group` 与 `run_match` 的入参同名同义：它把"这份文件里的哪一套条件"
    摘出来。为 None 表示这条候选**不按规则判定**（资助类政策在库里没有规则），
    只按原文写明的适用对象判断"要不要推荐给你"。

    `audience` 是 `(画像字段, 算子, 期望值)` 三元组，用 `matching.evaluate()`
    求值——不另写比较逻辑。它只决定"这条该不该出现在你的推荐里"，
    不决定"你可能符合还是暂不符合"。

    `audience_quote` 与 `condition_quote` 分开，是因为"这份文件管谁"和"这个奖项
    要什么条件"常常不在同一章：前者在总则第二条，后者在第二章。混用会让卡片把
    励志奖学金的五个条件当成国家奖学金的适用对象。

    `evidence_quote` 只在"理由引用的条文"与"条件所在的条文"不是同一条时才写：
    困难认定的理由是第十一条（与奖助挂钩），条件摘要是第九条（三档与有效期）。
    不写时默认与 `condition_quote` 相同——绝大多数条目都是同一条。
    """
    return {
        "key": key,
        "title": title,
        "category": category,
        "category_label": CATEGORY_LABEL[category],
        "kind": kind,
        "doc_key": doc_key,
        "target_group": target_group,
        "guide_key": guide_key,
        "audience": audience,
        "audience_quote": audience_quote,
        "condition_quote": condition_quote,
        "evidence_quote": evidence_quote or condition_quote,
        "conditions_from_quote": conditions_from_quote,
        "prereq_for": prereq_for,
        "note": note,
        "ask": ask or f"{title}需要满足哪些条件、准备哪些材料、怎么办理？",
    }


_SUBJECTS: list[dict] = [
    # ------------------------------------------------------------ 奖学金
    _subject(
        "national-grant", "国家奖学金", "scholarship", "award",
        "本科生奖学金评审办法",
        target_group="国家奖学金申请人",
        audience=(("student_type", OP_CONTAINS_ANY, "本科"),),
        audience_quote={"section": "第一章 总则", "article": "第二条"},
        # 全文提到"国家奖学金"的只有第四条（二）的兼得限制。把整条（励志的五个
        # 条件）当成国家奖学金的条件摆出来，比什么都不给更糟——所以这里只取
        # 含关键词的那一行做**溯源**，不用它当"主要申请条件"。
        condition_quote={"section": "第二章 申请条件", "lines": "国家奖学金"},
        conditions_from_quote=False,
        note=(
            "本办法没有单列国家奖学金的评审条件，全文提到它的只有第四条（二）"
            "「同一学年内未获得国家奖学金」这一兼得限制，具体条件以国家文件与当年通知为准。"
        ),
        ask="国家奖学金的申请条件、评审流程和材料是什么？",
    ),
    _subject(
        "inspiring-scholarship", "国家励志奖学金", "scholarship", "award",
        "本科生奖学金评审办法",
        target_group="国家励志奖学金申请人",
        guide_key="inspiring-scholarship",
        audience=(("student_type", OP_CONTAINS_ANY, "本科"),),
        audience_quote={"section": "第一章 总则", "article": "第二条"},
        condition_quote={"section": "第二章 申请条件", "article": "第四条"},
        ask="国家励志奖学金需要满足哪些条件、准备哪些材料？",
    ),
    _subject(
        "excellent-scholarship", "校级优秀学生奖学金", "honors", "award",
        "本科生奖学金评审办法",
        target_group="校级优秀学生奖学金申请人",
        guide_key="excellent-scholarship",
        audience=(("student_type", OP_CONTAINS_ANY, "本科"),),
        audience_quote={"section": "第一章 总则", "article": "第二条"},
        condition_quote={"section": "第二章 申请条件", "article": "第五条"},
        ask="校级优秀学生奖学金（优秀学生评选）的条件和流程是什么？",
    ),
    # ------------------------------------------------------- 资助与服务
    _subject(
        "hardship-certification", "家庭经济困难学生认定", "support", "support",
        "国家助学贷款与勤工助学管理办法",
        guide_key="hardship-certification",
        # 认定面向全体学生开放（每学年重新认定），所以不设适用对象门槛，
        # 也没有"适用对象"原文可引——文件里没单列这一句。
        # 条件摘要取第九条（认定分三档、每学年 9 月集中受理、有效期一年）；
        # 理由引用的是第十一条（认定与奖助挂钩）。两条同属第五章，但**不能混用**：
        # 把"挂钩关系"摆在"主要申请条件"里，学生会以为那就是认定标准。
        condition_quote={"section": "第五章 家庭经济困难学生认定", "article": "第九条"},
        evidence_quote={"section": "第五章 家庭经济困难学生认定", "article": "第十一条"},
        # 这三条政策都以"已认定困难"为前提；学生卡在这里时，这张卡片要指出
        # 它是哪几件事的前置条件，而不是笼统地说"你可以申请"。
        prereq_for=("inspiring-scholarship", "origin-student-loan", "work-study"),
        note=(
            "原文未单列适用对象：认定面向全校在籍学生，每学年 9 月集中受理一次，"
            "结果有效期为一年（第九条）。"
        ),
        ask="家庭经济困难学生认定需要哪些材料、什么时候办理？",
    ),
    _subject(
        "origin-student-loan", "生源地信用助学贷款", "support", "support",
        "国家助学贷款与勤工助学管理办法",
        guide_key="origin-student-loan",
        audience=(("poverty_certified", OP_EQUALS, "yes"),),
        audience_quote={"section": "第一章 生源地信用助学贷款", "article": "第一条"},
        # 「管谁」和「怎么办」在这份文件里是两条：第一条写对象与额度，
        # 第三条写办理期与凭证。两行指向同一条会让卡片看起来在自我复读。
        condition_quote={"section": "第一章 生源地信用助学贷款", "article": "第三条"},
        ask="生源地信用助学贷款怎么申请、什么时候办理？",
    ),
    _subject(
        "work-study", "勤工助学", "support", "support",
        "国家助学贷款与勤工助学管理办法",
        guide_key="work-study",
        audience=(("poverty_certified", OP_EQUALS, "yes"),),
        audience_quote={"section": "第三章 勤工助学", "article": "第七条"},
        # 第七条讲"怎么申请、优先谁"，第十五条讲上岗前的要求（岗前培训、工时表）。
        # 这两段才是学生真正要准备的东西，所以条件摘要取第十五条。
        condition_quote={"section": "第七章 常见问题", "article": "第十五条"},
        note=(
            "原文是「优先安排家庭经济困难学生」而不是「仅限」，未认定的学生同样可以申请；"
            "这里只在已认定困难时推荐，是推荐范围的取舍，不是资格门槛。"
        ),
        ask="勤工助学岗位怎么申请、报酬怎么发？",
    ),
    _subject(
        "postgraduate-recommend", "推荐免试攻读研究生（推免）", "service", "support",
        "推荐免试攻读研究生遴选办法",
        guide_key="postgraduate-recommend",
        # 原文写"应届毕业生"，画像里能对应的是"本科四年级"，只在四年级推荐。
        audience=(("student_type", OP_CONTAINS_ANY, "本科"), ("grade", OP_GTE, "4")),
        # 第三条整条都是条件，但"适用对象"只该取其中（一）那一行：
        # 把五条条件再贴到"适用对象"里，学生分不清哪句在说他、哪句在要求他。
        audience_quote={"section": "第二章 申请条件", "article": "第三条", "lines": "（一）"},
        condition_quote={"section": "第二章 申请条件", "article": "第三条"},
        note=(
            "原文第三条还要求「前六个学期无不及格课程」「全国大学英语六级不低于 425 分」；"
            "这两项不在画像字段里，请自行核对成绩单。"
        ),
        ask="推免需要满足什么条件、什么时候申请？",
    ),
]

_BY_KEY = {spec["key"]: spec for spec in _SUBJECTS}

# 需求点名"优先包括"的五类，逐类交代落在哪张卡片上——**包括落不上的那一类**。
# 这张表是给人核对覆盖用的，不是给前端渲染卡片用的。
PRIORITY = (
    ("国家奖学金", "national-grant", "原文只有兼得限制，卡片给「暂无法判断」"),
    ("国家励志奖学金", "inspiring-scholarship", "5 条已复核条件，可完整判定三态"),
    ("校级奖学金", "excellent-scholarship", "原文条件齐全，但 3 条规则被退回，不做资格判定"),
    ("助学金", None, "知识库尚未收录国家助学金评审办法，不生成卡片"),
    ("优秀学生评选", "excellent-scholarship", "本校的评优奖项即校级优秀学生奖学金"),
)


# ---------------------------------------------------------------- 画像
def _latest_session(user) -> MatchSession | None:
    """该用户最近一次匹配提交的记录。

    同一秒内可能有两条（脚本连续调用），所以再按 id 兜一层：ULID 单调递增，
    取大的那条就是后写的。没有这一层，推荐会随机取到较早的那份画像。
    """
    return db.session.execute(
        db.select(MatchSession)
        .where(MatchSession.user_id == user.id)
        .order_by(MatchSession.created_at.desc(), MatchSession.id.desc())
        .limit(1)
    ).scalars().first()


def _profile_of(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _filled(field: str, profile: dict) -> bool:
    return str(profile.get(field) or "").strip() not in ("", "None")


def _profile_view(session: MatchSession | None, docs_by_id: dict) -> dict:
    profile = _profile_of(session.profile_json) if session else {}
    missing = [field for field in PROFILE_FIELDS if not _filled(field, profile)]
    doc = docs_by_id.get(session.document_id) if session else None
    return {
        "source": "latest_match" if session else "none",
        "session_id": session.id if session else None,
        "document_id": session.document_id if session else None,
        "document_title": doc.title if doc else None,
        "submitted_at": session.created_at.isoformat() + "Z" if session else None,
        "fields": {
            field: profile.get(field) for field in PROFILE_FIELDS if _filled(field, profile)
        },
        "filled": len(PROFILE_FIELDS) - len(missing),
        "total": len(PROFILE_FIELDS),
        "completeness": round((len(PROFILE_FIELDS) - len(missing)) / len(PROFILE_FIELDS), 3),
        "missing_fields": missing,
        "missing_labels": [FIELD_LABELS.get(field, field) for field in missing],
    }


# ---------------------------------------------------------------- 适用对象
def _value_label(value) -> str:
    text = str(value if value is not None else "").strip()
    return VALUE_LABEL.get(text.lower(), text)


def _audience_rules(spec: dict) -> list:
    """把编目里的三元组变成 `evaluate()` 能吃的规则对象。

    刻意构造 `SimpleNamespace` 而不是 `PolicyRule`：这些条件**不是**从文件里抽出来
    的规则，不该出现在规则管理页、也不该被当成"已复核"。它们只是编目时对原文
    适用对象的一句话翻译，作用仅限于"这条政策该不该出现在这个学生的推荐里"。
    """
    rules = []
    for index, (field, operator, expected) in enumerate(spec["audience"], start=1):
        rules.append(SimpleNamespace(
            rule_code=f"AU-{index}",
            rule_type="eligibility",
            field_key=field,
            operator=operator,
            expected_value=expected,
            description=(
                f"{FIELD_LABELS.get(field, field)}{OP_TEXT.get(operator, operator)}"
                f"{_value_label(expected)}"
            ),
            original_quote="",
            page_or_section=None,
            target_group=None,
            chunk_id=None,
        ))
    return rules


def _audience_check(spec: dict, profile: dict) -> dict:
    """返回 `{status, outcomes, missing}`。status 只用来决定"推不推"。

    - `met`：画像能确认属于适用对象 → 正常推荐；
    - `unknown`：画像缺字段，无法确认 → **仍然推荐**，并在卡片上写明缺什么
      （需求 4.3：信息不完整时可以推荐，但要说明缺哪些信息）；
    - `not_met`：画像能确认**不属于**适用对象 → 不进推荐列表，记进 `excluded`，
      前端可以展开看到"为什么没给你推"。
    """
    rules = _audience_rules(spec)
    outcomes = [evaluate(rule, profile) for rule in rules]
    missing = [
        rule.field_key
        for rule, outcome in zip(rules, outcomes)
        if outcome.get("unknown_reason") == UNKNOWN_MISSING_INFO
    ]
    if any(o["status"] == MATCH_NOT_MET for o in outcomes):
        status = MATCH_NOT_MET
    elif any(o["status"] == MATCH_UNKNOWN for o in outcomes):
        status = MATCH_UNKNOWN
    else:
        status = MATCH_MET
    return {"status": status, "outcomes": outcomes, "missing": missing}


# ---------------------------------------------------------------- 原文
def _quote_block(doc, chunks: list, spec: dict | None) -> dict | None:
    """按编目指向取一段原文。取不到就返回 None——**不猜、不拼**。"""
    if not spec:
        return None
    chunk = _find_chunk(chunks, spec.get("section"))
    if chunk is None:
        return None
    text = _clean(chunk.content, chunk.section_title, doc.title)
    article = spec.get("article")
    if article:
        sliced = _slice_article(text, article)
        # 条文没找到就退回整章，但把 article 置空：卡片上不能写"第四条"却给整章
        text = sliced or text
        article = article if sliced else None
    needle = spec.get("lines")
    if needle:
        kept = [line for line in text.split("\n") if needle in line]
        if kept:
            text = "\n".join(kept)
    if not text.strip():
        return None
    return {
        "chunk_id": chunk.id,
        "page": chunk.page_number,
        "section": chunk.section_title or spec.get("section"),
        "article": article,
        "text": text,
        "quote": text[:300],
    }


def _preview(text: str, limit: int = 160) -> str:
    """把原文压成卡片上的一行摘要：按分句累加，不把一个条件切两半。"""
    flat = " ".join(text.split())
    parts: list[str] = []
    size = 0
    for piece in flat.replace("。", "。\n").replace("；", "；\n").split("\n"):
        piece = piece.strip()
        if not piece:
            continue
        if size + len(piece) > limit and parts:
            break
        parts.append(piece)
        size += len(piece)
    out = "".join(parts) if parts else flat[:limit]
    if len(out) > limit:
        out = out[:limit].rstrip("；。，, ")
    if len(out) < len(flat):
        out = out.rstrip("；。，, ") + "…"
    return out


def _rule_evidence(rule: PolicyRule, chunks_by_id: dict) -> dict:
    chunk = chunks_by_id.get(rule.chunk_id)
    return {
        "chunk_id": rule.chunk_id,
        "page": chunk.page_number if chunk else None,
        "section": chunk.section_title if chunk else rule.page_or_section,
        "quote": (rule.original_quote or "")[:300],
    }


# ---------------------------------------------------------------- 判定
def _condition_rows(rules: list, outcomes: list, chunks_by_id: dict) -> list[dict]:
    rows = []
    for rule, outcome in zip(rules, outcomes):
        rows.append({
            "rule_code": rule.rule_code,
            "field_key": rule.field_key,
            "description": rule.description,
            "status": outcome["status"],
            "status_label": CONDITION_LABEL[outcome["status"]],
            "unknown_reason": outcome["unknown_reason"],
            "user_value": outcome["user_value"],
            "expected_value": outcome["expected_value"],
            "rationale": outcome["rationale"],
            "evidence": _rule_evidence(rule, chunks_by_id),
        })
    return rows


def _aggregate(outcomes: list) -> str:
    """三态聚合。顺序就是需求 3 的措辞顺序：先看有没有明确不符合，再看有没有判不了。"""
    if not outcomes:
        return MATCH_UNKNOWN
    if any(o["status"] == MATCH_NOT_MET for o in outcomes):
        return MATCH_NOT_MET
    if any(o["status"] == MATCH_UNKNOWN for o in outcomes):
        return MATCH_UNKNOWN
    return MATCH_MET


def _rule_gap(specific: list, generic: list, other_approved: list) -> tuple[str, str]:
    """没有可判定规则时，说清**为什么没有**。

    四种"没有"完全不同：被退回（试过、没通过）、待复核（还没人看）、
    规则全在别的奖项上（文件有规则，但不是给你的）、一条都没有（库里就没抽）。
    混成一句"暂无条件"会让管理员无从下手。

    计数只算**本奖项的**规则（`target_group` 完全相等）。把未标注奖项的通用
    规则也算进来，会出现"国家奖学金有 1 条规则被退回"这种半真半假的话——
    那条其实是「交换生不适用本办法」，与这个奖项没有关系。
    """
    spec_rejected = [r for r in specific if r.review_status == REVIEW_REJECTED]
    spec_pending = [r for r in specific if r.review_status == REVIEW_NEEDS]
    if spec_rejected:
        return (
            "rules_rejected",
            f"该奖项的 {len(spec_rejected)} 条条件规则未通过复核，系统不据此判定资格。",
        )
    if spec_pending:
        return (
            "rules_pending",
            f"该奖项的 {len(spec_pending)} 条条件规则待管理员复核，系统不据此判定资格。",
        )
    if other_approved:
        groups = "、".join(sorted({
            # "国家励志奖学金申请人" → "国家励志奖学金"：卡片上给人看的是奖项名，
            # 不是规则库里的筛选标签
            (r.target_group or "").strip().removesuffix("申请人") or (r.target_group or "")
            for r in other_approved if r.target_group
        })) or "其他奖项"
        return (
            "rules_other_group",
            f"本办法的 {len(other_approved)} 条已复核条件规则全部针对{groups}，"
            f"没有针对本奖项的规则。",
        )
    if generic:
        return (
            "generic_unreviewed",
            f"本办法的 {len(generic)} 条通用条件未通过复核，且没有针对本奖项的规则，"
            f"需人工核实。",
        )
    return ("no_rules", "知识库里没有这份文件的条件规则，需人工核实。")


def _award_verdict(specific: list, generic: list, other_approved: list, profile: dict,
                   chunks_by_id: dict) -> dict:
    approved = [
        rule for rule in specific + generic if rule.review_status == REVIEW_APPROVED
    ]

    outcomes = [evaluate(rule, profile) for rule in approved]
    rows = _condition_rows(approved, outcomes, chunks_by_id)
    status = _aggregate(outcomes)

    met = [row for row in rows if row["status"] == MATCH_MET]
    not_met = [row for row in rows if row["status"] == MATCH_NOT_MET]
    unknown = [row for row in rows if row["status"] == MATCH_UNKNOWN]
    missing = sorted({
        rule.field_key
        for rule, outcome in zip(approved, outcomes)
        if outcome.get("unknown_reason") == UNKNOWN_MISSING_INFO
    })

    if not approved:
        code, text = _rule_gap(specific, generic, other_approved)
        reason = {
            "code": code,
            "text": text,
            "highlights": [
                r.description
                for r in specific
                if r.review_status in (REVIEW_REJECTED, REVIEW_NEEDS)
            ][:3],
        }
    elif status == MATCH_MET:
        reason = {
            "code": "conditions_met",
            "text": f"{len(met)} 项已复核条件全部符合。",
            "highlights": [row["rationale"] for row in met][:3],
        }
    elif status == MATCH_NOT_MET:
        tail = f"，其余 {len(met)} 项符合" if met else ""
        joined = "；".join(row["description"].strip("。； ") for row in not_met)
        reason = {
            "code": "conditions_partial" if met else "conditions_failed",
            "text": f"{len(not_met)} 项条件不符合{tail}：{_preview(joined, 80)}",
            "highlights": [row["rationale"] for row in not_met][:3],
        }
    else:
        labels = [FIELD_LABELS.get(field, field) for field in missing]
        reason = {
            "code": "missing_info",
            "text": (
                f"还缺 {len(labels)} 项信息：{'、'.join(labels)}，"
                f"补齐后可判定 {len(unknown)} 项条件。"
                if labels else
                f"{len(unknown)} 项条件需人工核实，系统不做自动判定。"
            ),
            "highlights": [row["rationale"] for row in unknown][:3],
        }

    return {
        "status": status,
        "conditions": rows,
        "counts": {
            "met": len(met),
            "not_met": len(not_met),
            "unknown": len(unknown),
            "approved": len(approved),
            "pending": len([r for r in specific if r.review_status == REVIEW_NEEDS]),
            "rejected": len([r for r in specific if r.review_status == REVIEW_REJECTED]),
        },
        "missing_fields": missing,
        "reason": reason,
        "judged": bool(approved),
    }


def _support_verdict(spec: dict, profile: dict, audience: dict,
                     quote: dict | None) -> dict:
    """资助/服务类：不做资格判定，只说明"为什么推荐给你"。

    它们在库里没有已复核规则（这些文件的 `policy_rules` 一条都没有），所以按
    需求 4.2 只能给「暂无法判断」。但"暂无法判断"不等于"没什么可说"——原文写明
    的适用对象、前置条件、办理时间都是真实的，卡片要把这些摆出来。

    `quote` 必须是**适用对象**那段原文，不能拿条件摘要那段顶上：
    理由说的是"你的画像与文件第几条写明的适用对象一致"，引用就得是那一句。
    （助学贷款的适用对象在第一条、办理要求挪到了第三条，两段一旦混用，
    卡片就会声称"第三条写的是适用对象"。）
    """
    missing = audience["missing"]
    quote_text = (quote or {}).get("text", "")
    section = (quote or {}).get("section") or ""
    article = (quote or {}).get("article") or ""
    where = f"{section} {article}".strip()

    if spec["key"] == "hardship-certification":
        if profile.get("poverty_certified") == "yes":
            reason = {
                "code": "audience_matched",
                "text": "你已通过家庭经济困难认定；认定结果有效期为一年，每学年 9 月需重新认定。",
                "highlights": [],
            }
        else:
            reason = {
                "code": "prereq_gap",
                "text": (
                    "你尚未通过家庭经济困难认定。文件第十一条写明：认定结果与奖学金评定、"
                    "助学贷款办理、勤工助学岗位分配挂钩。"
                ),
                "highlights": [],
            }
    elif missing:
        labels = [FIELD_LABELS.get(field, field) for field in missing]
        reason = {
            "code": "missing_info",
            "text": f"还缺 {len(labels)} 项信息（{'、'.join(labels)}），补上后才能确认是否适用于你。",
            "highlights": [],
        }
    else:
        reason = {
            "code": "audience_matched",
            "text": (
                f"你的画像与文件{where}写明的适用对象一致。"
                if where else "文件写明的适用对象与你的画像一致。"
            ),
            "highlights": [quote_text[:90]] if quote_text else [],
        }

    return {
        "status": MATCH_UNKNOWN,
        "conditions": [],
        "counts": {"met": 0, "not_met": 0, "unknown": 0,
                   "approved": 0, "pending": 0, "rejected": 0},
        "missing_fields": missing,
        "reason": reason,
        "judged": False,
    }


# ---------------------------------------------------------------- 组装
class _Context:
    def __init__(self, docs, chunks_by_doc, rules_by_doc):
        self.docs = docs
        self.chunks_by_doc = chunks_by_doc
        self.rules_by_doc = rules_by_doc
        self.chunks_by_id = {
            chunk.id: chunk for group in chunks_by_doc.values() for chunk in group
        }
        self.docs_by_id = {doc.id: doc for doc in docs}


def _load_all_rules() -> dict[str, list[PolicyRule]]:
    """一次取齐**全部**规则（含待复核与被退回）。

    指南只要已复核的；推荐必须知道"被退回了几条"，否则无法解释"为什么这个
    奖项一条规则都没有"。多一次查询换一句能落地的解释，值得。
    """
    grouped: dict[str, list[PolicyRule]] = {}
    for rule in db.session.execute(db.select(PolicyRule)).scalars():
        grouped.setdefault(rule.document_id, []).append(rule)
    return grouped


def _detail_action(spec: dict, doc) -> dict:
    """详情入口：有指南走指南，没有就走知识库里的那份文件。

    国家奖学金没有独立的指南事项，硬塞一个 `/guides/national-grant` 只会 404；
    知识库的 `?doc=` 是既有能力（问答页的"在知识库中打开"就在用），深链过去
    会展开那份文件的全部片段——这才是"政策详情"。
    """
    if spec["guide_key"]:
        return {
            "available": True, "kind": "guide", "guide_key": spec["guide_key"],
            "path": f"/guides/{spec['guide_key']}",
            "document_id": doc.id if doc else None, "reason": None,
        }
    if doc is not None:
        return {
            "available": True, "kind": "document", "guide_key": None,
            "path": f"/library?doc={doc.id}",
            "document_id": doc.id, "reason": None,
        }
    return {
        "available": False, "kind": None, "guide_key": None, "path": None,
        "document_id": None, "reason": "该政策还没有收录可查阅的文件",
    }


def _build_card(spec: dict, ctx: _Context, profile: dict, audience: dict, doc,
                audience_block: dict | None, condition_block: dict | None,
                evidence_block: dict | None) -> dict:
    chunks = ctx.chunks_by_doc.get(doc.id, [])
    rules = ctx.rules_by_doc.get(doc.id, [])
    target = spec["target_group"]
    if target is None:
        # 资助/服务类不按规则判定：这些文件在库里一条规则都没有，
        # 硬把别的文件的规则拿过来只会张冠李戴。
        specific, generic = [], []
    else:
        specific = [
            rule for rule in rules
            if rule.rule_type in CONDITION_RULE_TYPES
            and (rule.target_group or "").strip() == target
        ]
        # 未标注奖项的规则是**通用条件**（如"须为在籍本科生"），与 `run_match`
        # 的过滤口径一致：它们对同一份文件里的所有奖项都成立。
        generic = [
            rule for rule in rules
            if rule.rule_type in CONDITION_RULE_TYPES
            and not (rule.target_group or "").strip()
        ]
    other_approved = [
        rule for rule in rules
        if rule.rule_type in CONDITION_RULE_TYPES
        and rule.review_status == REVIEW_APPROVED
        and (rule.target_group or "").strip() not in ("", target)
    ]

    if spec["kind"] == "award":
        verdict = _award_verdict(
            specific, generic, other_approved, profile, ctx.chunks_by_id
        )
    else:
        verdict = _support_verdict(spec, profile, audience, audience_block)

    approved_conditions = [
        rule for rule in specific + generic if rule.review_status == REVIEW_APPROVED
    ]
    plan_rules = [
        rule for rule in rules
        if rule.review_status == REVIEW_APPROVED
        and rule.rule_type in (RULE_MATERIAL, RULE_DEADLINE, RULE_PROCESS)
    ]

    if verdict["conditions"]:
        # 规则描述自己带句号，直接拼会得到"……学生。；申请……"。
        condition_text = _preview(
            "；".join(
                row["description"].strip("。；　 ")
                for row in verdict["conditions"]
            ), 150
        )
    elif condition_block and spec["conditions_from_quote"]:
        condition_text = _preview(condition_block["text"], 160)
    else:
        condition_text = ""

    # 「适用对象」和「主要申请条件」指向同一条条文时，卡片会把同一句话印两遍，
    # 看起来很像个 bug。这类编目错误不该靠人眼在卡片上发现：两段正文一模一样就
    # 只留"适用对象"，条件行退回"原文未单列申请条件"。
    if (
        condition_text
        and not verdict["conditions"]
        and audience_block
        and condition_block
        and condition_block["text"].strip() == audience_block["text"].strip()
    ):
        condition_text = ""

    # 计划的可生成性取决于**本奖项自己**有没有已复核条件规则：
    # `POST /plans/draft` 按文件出任务，不看 target_group。若某奖项没有自己的
    # 条件规则却允许生成，草稿里会混进同文件其他奖项的材料——那比不给按钮更糟。
    plan_available = bool(approved_conditions) and bool(plan_rules)
    if plan_available:
        plan_reason = None
    elif not approved_conditions:
        plan_reason = (
            "该奖项的条件规则尚未复核，生成计划会混入同文件里其他奖项的条款，因此不提供"
        )
    else:
        plan_reason = "该文件的申请材料、时间与流程条款尚未复核，暂时无法生成办理计划"

    match_available = bool(approved_conditions)
    match_reason = None if match_available else (
        "该奖项的条件规则尚未通过复核，资格匹配暂时无法比对；可先看原文条件或咨询学院"
    )

    return {
        "key": spec["key"],
        "title": spec["title"],
        "category": spec["category"],
        "category_label": spec["category_label"],
        "kind": spec["kind"],
        "status": verdict["status"],
        "status_label": STATUS_LABEL[verdict["status"]],
        "reason": verdict["reason"],
        "conditions": verdict["conditions"],
        "conditions_preview": condition_text,
        "conditions_available": bool(condition_text),
        "conditions_source": (
            "rules" if verdict["conditions"]
            else ("policy" if condition_text else None)
        ),
        "condition_counts": verdict["counts"],
        "missing_fields": verdict["missing_fields"],
        "missing_labels": [FIELD_LABELS.get(f, f) for f in verdict["missing_fields"]],
        "audience": {
            "text": audience_block["text"][:200],
            "section": audience_block["section"],
            "article": audience_block["article"],
            "quote": audience_block["quote"],
        } if audience_block else None,
        "evidence": evidence_block,
        "document": _doc_meta(doc, ctx.docs) if doc else None,
        "target_group": spec["target_group"],
        "note": spec["note"],
        "ask": spec["ask"],
        "actions": {
            "detail": _detail_action(spec, doc),
            "match": {
                "available": match_available,
                "document_id": doc.id if doc else None,
                "target_group": spec["target_group"],
                "approved_condition_rules": len(approved_conditions),
                "reason": match_reason,
            },
            "plan": {
                "available": plan_available,
                "document_id": doc.id if doc else None,
                "approved_rules": len(plan_rules),
                "reason": plan_reason,
            },
        },
        "_judged": verdict["judged"],
        # 只有**真的写了适用对象条件**的候选才算"画像与适用对象一致"：
        # 困难认定对所有人开放，`_audience_check` 会返回 met，但那不是一条匹配到的证据。
        "_audience_ok": bool(spec["audience"]) and audience["status"] == MATCH_MET,
        "_active": bool(doc) and doc.effective_status == EFFECTIVE_ACTIVE,
    }


def _score(card: dict) -> int:
    counts = card["condition_counts"]
    score = STATUS_SCORE[card["status"]]
    score += min(counts["met"], 5) * 6
    if card["_audience_ok"]:
        score += 20
    if card["_active"]:
        score += 15
    score += CATEGORY_SCORE.get(card["category"], 0)
    if not card["_judged"] and card["kind"] == "award":
        score -= 25
    if card["reason"]["code"] == "prereq_gap":
        score += PREREQ_BONUS
    return score


def _sort_basis(card: dict) -> list[str]:
    """排序依据，逐条写出来给用户看——"为什么它排第一"不该是黑盒。"""
    basis = [card["status_label"]]
    counts = card["condition_counts"]
    if counts["met"]:
        basis.append(f"{counts['met']} 项已复核条件符合")
    if card["_audience_ok"]:
        basis.append("画像与适用对象一致")
    if card["_active"]:
        basis.append("政策现行有效")
    if card["reason"]["code"] == "prereq_gap":
        basis.append("是其他政策的前置条件")
    return basis


def _prereq_hint(card: dict, cards_by_key: dict) -> None:
    """把"这张卡是哪几件事的前置条件"补进理由。

    判据只有一条：某条政策有**明确不符合**的规则，且那条规则的字段是
    `poverty_certified`。点的名是同一批规则算出来的，不是猜的。
    """
    if card["key"] != "hardship-certification":
        return
    affected = []
    for key in _BY_KEY[card["key"]]["prereq_for"]:
        other = cards_by_key.get(key)
        if not other or other["status"] != MATCH_NOT_MET:
            continue
        if any(
            row["status"] == MATCH_NOT_MET and row["field_key"] == "poverty_certified"
            for row in other["conditions"]
        ):
            affected.append(other["title"])
    if affected:
        card["reason"]["text"] += f" 你目前在「{'、'.join(affected)}」上正卡在这一项。"


def build_recommendations(user) -> dict:
    docs, chunks_by_doc, _approved_only = _load_context()
    ctx = _Context(docs, chunks_by_doc, _load_all_rules())
    session = _latest_session(user)
    profile = _profile_of(session.profile_json) if session else {}
    profile_view = _profile_view(session, ctx.docs_by_id)

    cards: list[dict] = []
    excluded: list[dict] = []
    for spec in _SUBJECTS:
        doc = _pick_document(ctx.docs, spec["doc_key"])
        if doc is None:
            excluded.append({
                "key": spec["key"], "title": spec["title"],
                "code": "missing_policy",
                "reason": f"知识库里没有《{spec['doc_key']}》，无法给出有依据的推荐",
            })
            continue
        audience = _audience_check(spec, profile)
        if audience["status"] == MATCH_NOT_MET:
            # 未推荐的原因要能直接读懂。`evaluate()` 的 rationale 是给匹配页用的
            # （"家庭经济困难认定为「no」，要求等于「yes」"），在这里读起来像报错，
            # 所以用同一批算子重新组织一句人话——判断仍然来自 evaluate，没有另算一遍。
            reason = "画像显示不属于该政策的适用对象"
            for rule, outcome in zip(_audience_rules(spec), audience["outcomes"]):
                if outcome["status"] == MATCH_NOT_MET:
                    label = FIELD_LABELS.get(rule.field_key, rule.field_key)
                    reason = (
                        f"你的「{label}」是「{_value_label(outcome['user_value'])}」，"
                        f"不满足该政策的适用对象要求（{rule.description}）。"
                    )
                    break
            excluded.append({
                "key": spec["key"], "title": spec["title"],
                "code": "out_of_scope", "reason": reason,
            })
            continue
        chunks = ctx.chunks_by_doc.get(doc.id, [])
        cards.append(_build_card(
            spec, ctx, profile, audience, doc,
            _quote_block(doc, chunks, spec["audience_quote"]),
            _quote_block(doc, chunks, spec["condition_quote"]),
            _quote_block(doc, chunks, spec["evidence_quote"]),
        ))

    by_key = {card["key"]: card for card in cards}
    for card in cards:
        _prereq_hint(card, by_key)

    for card in cards:
        card["score"] = _score(card)
    cards.sort(key=lambda card: (-card["score"], card["key"]))
    for index, card in enumerate(cards, start=1):
        card["rank"] = index
        card["sort_basis"] = _sort_basis(card)

    for card in cards:
        for field in ("_judged", "_audience_ok", "_active"):
            card.pop(field, None)

    counts = {MATCH_MET: 0, MATCH_NOT_MET: 0, MATCH_UNKNOWN: 0}
    for card in cards:
        counts[card["status"]] += 1

    coverage = []
    for label, key, note in PRIORITY:
        if key is None:
            coverage.append({
                "label": label, "status": "missing", "subject_key": None, "note": note,
            })
            continue
        card = by_key.get(key)
        coverage.append({
            "label": label,
            "status": "covered" if card else "excluded",
            "subject_key": key,
            "note": note if card else "该政策未进入本次推荐（见「未推荐」折叠区）",
        })

    notices = []
    if profile_view["source"] == "none":
        notices.append({
            "level": "warn",
            "code": "no_profile",
            "text": "还没有画像记录：先到「资格匹配」填一次画像并提交，推荐就会按你的条件重新排序。",
        })
    elif profile_view["missing_fields"]:
        notices.append({
            "level": "info",
            "code": "partial_profile",
            "text": (
                f"画像已填 {profile_view['filled']}/{profile_view['total']} 项，"
                f"缺 {'、'.join(profile_view['missing_labels'])}；"
                f"这些字段相关的条件只能判成「暂无法判断」。"
            ),
        })
    if excluded:
        notices.append({
            "level": "info",
            "code": "excluded",
            "text": f"有 {len(excluded)} 条政策因画像不符适用对象未列入推荐，可展开查看原因。",
        })

    return {
        "profile": profile_view,
        "summary": {
            "total": len(cards),
            "met": counts[MATCH_MET],
            "not_met": counts[MATCH_NOT_MET],
            "unknown": counts[MATCH_UNKNOWN],
            "cataloged": len(_SUBJECTS),
            "excluded": len(excluded),
            "empty": not cards,
        },
        "items": cards,
        "excluded": excluded,
        "coverage": coverage,
        "notices": notices,
        "empty_title": "暂未发现匹配度较高的政策",
        "empty_hint": (
            "知识库里还没有能对应到你当前情况的政策。可以先到「校园事务指南」按分类浏览，"
            "或补全画像后重新推荐。"
        ),
        "disclaimer": DISCLAIMER,
        "generated_at": utcnow().isoformat() + "Z",
    }


# ---------------------------------------------------------------- 路由
@bp.get("/recommendations")
@login_required
def recommendations():
    """为当前学生推荐政策。

    只读：不落库、不建会话、不改任何一行。反复刷新得到的是同一份结果
    （除非画像、规则或政策文件本身变了）——这正是"推荐可复现"的含义。
    """
    return ok(build_recommendations(current_user()))
