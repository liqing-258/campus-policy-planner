"""三态匹配（M5 中段）。

**这里一个模型调用都没有**，是刻意的（设计说明书 §7.6）：

资格判断会直接影响一个学生能不能申请到奖助学金，
所以它必须**可复现、可解释、可断言**。同一个画像跑两次得到不同结论，
既无法写测试，也无法向用户解释"为什么我不符合"。

三态不是"通过/不通过/出错"，而是三种**语义不同**的结果：

| 状态 | 含义 | 用户该做什么 |
| --- | --- | --- |
| `met` | 条件齐全、比较通过 | 无需操作 |
| `not_met` | 条件齐全、比较不通过 | 想办法达标，或换项目 |
| `unknown` | **无法判定**，且必须说明原因 | `missing_info` 补字段；`ambiguous_rule` 找人工 |

把 `unknown` 强判成 `not_met` 是最常见的偷懒做法，也是最伤用户的：
它会把"你没填绩点"说成"你绩点不够"。
"""

from __future__ import annotations

import json
import re

from flask import Blueprint

from app.auth import current_user, login_required
from app.contract import body_json, new_ulid, ok
from app.documents import _get_active_or_404
from app.errors import ApiError, Forbidden, RulesNotReady
from app.models import DocumentChunk, MatchResultItem, MatchSession, PolicyRule, db
from app.schemas import MatchRequest, validate
from shared.codes import DOCUMENT_NOT_FOUND
from shared.enums import (
    CONDITION_RULE_TYPES,
    MATCH_MET,
    MATCH_NOT_MET,
    MATCH_UNKNOWN,
    OP_CONTAINS_ANY,
    OP_EQUALS,
    OP_EXISTS,
    OP_GTE,
    OP_IN,
    OP_LTE,
    OP_MANUAL_REVIEW,
    OP_NOT_EQUALS,
    REVIEW_APPROVED,
    UNKNOWN_AMBIGUOUS_RULE,
    UNKNOWN_MISSING_INFO,
    UNKNOWN_NO_EVIDENCE,
)

bp = Blueprint("matching", __name__)

# 画像字段的中文名：理由里写 "gpa=2.5" 对学生没有意义，写"平均学分绩点 2.5"才有
FIELD_LABELS = {
    "student_type": "学生类型",
    "grade": "年级",
    "gpa": "平均学分绩点",
    "rank_percent": "专业排名百分比",
    "disciplinary_record": "纪律处分记录",
    "poverty_certified": "家庭经济困难认定",
    "major": "专业",
    "credits_earned": "已修学分",
    "enrollment_status": "学籍状态",
}

OP_TEXT = {
    OP_GTE: "不低于",
    OP_LTE: "不高于",
    OP_EQUALS: "等于",
    OP_NOT_EQUALS: "不等于",
    OP_IN: "属于",
    OP_CONTAINS_ANY: "包含",
    OP_EXISTS: "应当提供",
}

DISCLAIMER = (
    "匹配结果依据政策原文自动比对得出，仅用于辅助判断，不构成资格认定；"
    "最终结果以学校审核为准。标注“待人工核实”的条款需向相关部门确认。"
)

_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def _label(field_key: str | None) -> str:
    if not field_key:
        return "该条件"
    return FIELD_LABELS.get(field_key, field_key)


def _as_number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = _NUMBER.search(value)
        if match:
            return float(match.group())
    return None


def _as_list(value) -> list:
    """`[2, 3]` / `2,3` / `计算机|软件` 都能解析成列表。

    模型的 expected_value 是自由文本，这里多容错一点，比要求模型
    "严格输出 JSON 数组"现实得多——后者它经常做不到。
    """
    if value is None:
        return []
    if isinstance(value, list):
        return value
    text = str(value).strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
    except (ValueError, TypeError):
        pass
    for sep in ("、", ",", "，", "|", "/", ";"):
        if sep in text:
            return [part.strip() for part in text.split(sep) if part.strip()]
    return [text]


def _normalize_text(value) -> str:
    return str(value).strip().lower()


def evaluate(rule: PolicyRule, profile: dict) -> dict:
    """单条规则求值。返回 status / unknown_reason / user_value / expected_value / rationale。"""
    label = _label(rule.field_key)
    user_raw = profile.get(rule.field_key) if rule.field_key else None

    # 闸一：主观条款主动放弃自动判定
    if rule.operator == OP_MANUAL_REVIEW:
        return {
            "status": MATCH_UNKNOWN,
            "unknown_reason": UNKNOWN_AMBIGUOUS_RULE,
            "user_value": None,
            "expected_value": rule.expected_value,
            "rationale": (
                f"该条款为「{rule.description}」，属于无法量化的主观表述，"
                f"系统不做自动判定，请人工核实。"
            ),
        }

    # 闸二：规则自身缺算子或缺预期值 —— 这是规则库的问题，不是学生的问题
    if not rule.operator:
        return {
            "status": MATCH_UNKNOWN,
            "unknown_reason": UNKNOWN_AMBIGUOUS_RULE,
            "user_value": _stringify(user_raw),
            "expected_value": rule.expected_value,
            "rationale": f"规则「{rule.description}」没有可求值的算子，需管理员补齐后重新匹配。",
        }

    # 闸三：画像缺字段
    if user_raw is None or (isinstance(user_raw, str) and not user_raw.strip()):
        return {
            "status": MATCH_UNKNOWN,
            "unknown_reason": UNKNOWN_MISSING_INFO,
            "user_value": None,
            "expected_value": rule.expected_value,
            "rationale": (
                f"缺少字段 {rule.field_key}（{label}），无法判定「{rule.description}」；"
                f"请补充 {rule.field_key} 后重新匹配。"
            ),
        }

    operator = rule.operator
    expected_raw = rule.expected_value

    if operator == OP_EXISTS:
        return _result(True, user_raw, expected_raw,
                       f"{label}已提供（{_stringify(user_raw)}），满足「应当提供」要求。",
                       f"{label}未提供，不满足「应当提供」要求。")

    # 需要期望值才能比较的算子：缺了就退回 unknown，不许猜
    if expected_raw is None or str(expected_raw).strip() == "":
        return {
            "status": MATCH_UNKNOWN,
            "unknown_reason": UNKNOWN_AMBIGUOUS_RULE,
            "user_value": _stringify(user_raw),
            "expected_value": None,
            "rationale": f"规则「{rule.description}」的算子 {operator} 缺少比较值，无法判定。",
        }

    if operator in (OP_GTE, OP_LTE):
        user_num = _as_number(user_raw)
        expected_num = _as_number(expected_raw)
        if user_num is None or expected_num is None:
            return {
                "status": MATCH_UNKNOWN,
                "unknown_reason": UNKNOWN_AMBIGUOUS_RULE,
                "user_value": _stringify(user_raw),
                "expected_value": str(expected_raw),
                "rationale": (
                    f"{label}「{user_raw}」与阈值「{expected_raw}」无法按数值比较，需人工核实。"
                ),
            }
        met = user_num >= expected_num if operator == OP_GTE else user_num <= expected_num
        # 理由里必须出现两个数字：只说"不符合"，用户不知道差多少
        return _result(
            met, user_raw, expected_raw,
            f"{label} {_trim(user_num)} {_verdict(met)}要求 {OP_TEXT[operator]} {_trim(expected_num)}，符合。",
            f"{label} {_trim(user_num)} {_verdict(met)}要求 {OP_TEXT[operator]} {_trim(expected_num)}，不符合。",
        )

    if operator == OP_IN:
        options = [_normalize_text(x) for x in _as_list(expected_raw)]
        met = _normalize_text(user_raw) in options
        return _result(
            met, user_raw, expected_raw,
            f"{label}「{user_raw}」在允许取值 {'/'.join(str(o) for o in _as_list(expected_raw))} 内，符合。",
            f"{label}「{user_raw}」不在允许取值 {'/'.join(str(o) for o in _as_list(expected_raw))} 内，不符合。",
        )

    if operator == OP_CONTAINS_ANY:
        text = _normalize_text(user_raw)
        keys = [str(k) for k in _as_list(expected_raw)]
        met = any(_normalize_text(k) in text for k in keys)
        return _result(
            met, user_raw, expected_raw,
            f"{label}「{user_raw}」包含要求的任一关键词（{'/'.join(keys)}），符合。",
            f"{label}「{user_raw}」不包含要求的关键词（{'/'.join(keys)}），不符合。",
        )

    if operator == OP_NOT_EQUALS:
        met = _normalize_text(user_raw) != _normalize_text(expected_raw)
        return _result(
            met, user_raw, expected_raw,
            f"{label}「{user_raw}」不等于「{expected_raw}」，符合。",
            f"{label}「{user_raw}」等于「{expected_raw}」，不符合。",
        )

    # equals（默认分支）
    met = _normalize_text(user_raw) == _normalize_text(expected_raw)
    return _result(
        met, user_raw, expected_raw,
        f"{label}为「{user_raw}」，要求等于「{expected_raw}」，符合。",
        f"{label}为「{user_raw}」，要求等于「{expected_raw}」，不符合。",
    )


def _verdict(met: bool) -> str:
    return "满足" if met else "不满足"


def _result(met: bool, user_raw, expected_raw, met_text: str, not_met_text: str) -> dict:
    return {
        "status": MATCH_MET if met else MATCH_NOT_MET,
        "unknown_reason": None,
        "user_value": _stringify(user_raw),
        "expected_value": _stringify(expected_raw),
        "rationale": met_text if met else not_met_text,
    }


def _trim(number: float) -> str:
    return str(int(number)) if float(number).is_integer() else str(number)


def _stringify(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


# ---------------------------------------------------------------- 服务
def run_match(document_id: str, payload: dict, user) -> dict:
    data = validate(MatchRequest, payload)
    document = _get_active_or_404(document_id)

    rules = db.session.execute(
        db.select(PolicyRule)
        .where(PolicyRule.document_id == document.id,
               PolicyRule.review_status == REVIEW_APPROVED)
        .order_by(PolicyRule.rule_type, PolicyRule.rule_code)
    ).scalars().all()

    conditions = [r for r in rules if r.rule_type in CONDITION_RULE_TYPES]
    if not conditions:
        # 返回"0 符合 0 不符合"会让人以为"都不符合"，这里必须说清是规则没准备好
        raise RulesNotReady(
            "该文件还没有已复核的可比对规则；请管理员先抽取并复核条件类规则",
            payload={"document_id": document.id, "approved_rules": 0,
                     "total_rules": len(rules)},
        )

    # ---------- 按奖项过滤（M6，补 M5 登记的缺口）----------
    # 同一份《奖学金评审办法》里常同时写着励志奖学金（绩点 ≥ 3.0）与优秀学生奖学金
    # （绩点 ≥ 3.5）两套条件。不区分奖项时，一个绩点 3.2 的励志申请人会被优秀奖学金
    # 那条判成"不符合"——判错了人，而且理由看着还挺有理有据。
    # 规则没有标注 target_group 时一律保留：它是通用条件（如"须为在籍本科生"）。
    filtered_out = 0
    if data.target_group:
        target = data.target_group.strip()
        kept = [
            r for r in conditions
            if not (r.target_group or "").strip()
            or (r.target_group or "").strip() == target
        ]
        filtered_out = len(conditions) - len(kept)
        # 通用规则不能替某个奖项"背书"：如果一条明确标注该奖项的规则都没有，
        # 只剩通用规则也说明"这份文件里没写这个奖项的条件"。
        # 此时返回"1 符合"会让申请人以为自己的奖项稳了——比报错更危险。
        specific = [r for r in kept if (r.target_group or "").strip() == target]
        if not specific:
            raise RulesNotReady(
                f"该文件里没有针对「{data.target_group}」的已复核条件规则；"
                f"现有 {len(conditions)} 条条件规则里没有一条标注这个奖项，"
                f"请改选奖项或补充规则",
                payload={
                    "document_id": document.id,
                    "target_group": data.target_group,
                    "approved_rules": 0,
                    "condition_rules": len(conditions),
                    "generic_rules": len(kept),
                    "available_groups": sorted(
                        {r.target_group for r in conditions if (r.target_group or "").strip()}
                    ),
                },
            )
        conditions = kept

    profile = data.profile.model_dump(exclude_none=True)
    chunk_ids = {r.chunk_id for r in conditions}
    chunks = {
        c.id: c for c in db.session.execute(
            db.select(DocumentChunk).where(DocumentChunk.id.in_(chunk_ids))
        ).scalars().all()
    }

    session = MatchSession(
        id=new_ulid(), user_id=user.id, document_id=document.id,
        profile_json=json.dumps(profile, ensure_ascii=False),
        rule_snapshot_json=json.dumps(
            [{"id": r.id, "rule_code": r.rule_code, "operator": r.operator,
              "expected_value": r.expected_value} for r in conditions],
            ensure_ascii=False),
    )
    db.session.add(session)
    # 必须显式 flush 父行。MatchSession 与 MatchResultItem 之间**没有 relationship**
    # （数据模型 §3.7/3.8 只写了外键，没写 ORM 关系），而 SQLAlchemy 的 flush 依赖
    # 排序只来自 relationship，不来自表级外键。于是明细可能先于会话插入，
    # sqlite 的即时外键检查直接报 FOREIGN KEY constraint failed，整笔事务回滚，
    # 表现为 500——而不是"匹配失败"这种可解释的结果。
    # 2026-11-10 实测：不加这一行，match_sessions 落 0 行、match_result_items 报 FK。
    db.session.flush()

    items = []
    counts = {MATCH_MET: 0, MATCH_NOT_MET: 0, MATCH_UNKNOWN: 0}
    for rule in conditions:
        outcome = evaluate(rule, profile)
        counts[outcome["status"]] += 1
        chunk = chunks.get(rule.chunk_id)
        db.session.add(MatchResultItem(
            id=new_ulid(), session_id=session.id, rule_id=rule.id,
            status=outcome["status"], unknown_reason=outcome["unknown_reason"],
            user_value=outcome["user_value"], expected_value=outcome["expected_value"],
            rationale=outcome["rationale"], evidence_chunk_id=rule.chunk_id,
        ))
        items.append({
            "rule_id": rule.id,
            "rule_code": rule.rule_code,
            "rule_type": rule.rule_type,
            "description": rule.description,
            "status": outcome["status"],
            "unknown_reason": outcome["unknown_reason"],
            "user_value": outcome["user_value"],
            "expected_value": outcome["expected_value"],
            "rationale": outcome["rationale"],
            "evidence": {
                "chunk_id": rule.chunk_id,
                "page": chunk.page_number if chunk else None,
                "section": chunk.section_title if chunk else rule.page_or_section,
                "quote": (rule.original_quote or "")[:300],
            },
        })

    session.sum_met = counts[MATCH_MET]
    session.sum_not_met = counts[MATCH_NOT_MET]
    session.sum_unknown = counts[MATCH_UNKNOWN]
    db.session.commit()

    return {
        "session_id": session.id,
        "document_id": document.id,
        "summary": {
            "met": session.sum_met,
            "not_met": session.sum_not_met,
            "unknown": session.sum_unknown,
            "total": session.sum_met + session.sum_not_met + session.sum_unknown,
        },
        # 过滤了多少条、按什么奖项过滤的必须回传：否则"为什么只有 5 条"无从解释，
        # 而用户会以为自己漏填了信息。
        "target_group": data.target_group,
        "filtered_out": filtered_out,
        "items": items,
        "disclaimer": DISCLAIMER,
        "created_at": session.created_at.isoformat() + "Z",
    }


def load_session(session_id: str, user) -> dict:
    session = db.session.get(MatchSession, session_id)
    if session is None:
        # 契约里 GET /matches/{id} 的"不存在"用的是 2002；消息必须说清是匹配记录，
        # 否则与"文档不存在"混在一起，排查时会往错误方向走
        raise ApiError("匹配记录不存在", code=DOCUMENT_NOT_FOUND)
    if session.user_id != user.id:
        raise Forbidden("无权查看他人的匹配结果")

    rows = db.session.execute(
        db.select(MatchResultItem).where(MatchResultItem.session_id == session.id)
    ).scalars().all()
    rule_ids = [row.rule_id for row in rows]
    rules = {
        r.id: r for r in db.session.execute(
            db.select(PolicyRule).where(PolicyRule.id.in_(rule_ids))
        ).scalars().all()
    } if rule_ids else {}
    chunk_ids = [r.chunk_id for r in rules.values() if r.chunk_id]
    chunks = {
        c.id: c for c in db.session.execute(
            db.select(DocumentChunk).where(DocumentChunk.id.in_(chunk_ids))
        ).scalars().all()
    } if chunk_ids else {}

    items = []
    for row in rows:
        rule = rules.get(row.rule_id)
        chunk = chunks.get(row.evidence_chunk_id) if row.evidence_chunk_id else None
        items.append({
            "rule_id": row.rule_id,
            "rule_code": rule.rule_code if rule else None,
            "rule_type": rule.rule_type if rule else None,
            "description": rule.description if rule else None,
            "status": row.status,
            "unknown_reason": row.unknown_reason,
            "user_value": row.user_value,
            "expected_value": row.expected_value,
            "rationale": row.rationale,
            "evidence": {
                "chunk_id": row.evidence_chunk_id,
                "page": chunk.page_number if chunk else None,
                "section": chunk.section_title if chunk else None,
                "quote": (rule.original_quote or "")[:300] if rule else None,
            },
        })

    return {
        "session_id": session.id,
        "document_id": session.document_id,
        "profile": json.loads(session.profile_json or "{}"),
        "summary": {
            "met": session.sum_met,
            "not_met": session.sum_not_met,
            "unknown": session.sum_unknown,
            "total": session.sum_met + session.sum_not_met + session.sum_unknown,
        },
        "items": items,
        "disclaimer": DISCLAIMER,
        "created_at": session.created_at.isoformat() + "Z",
    }


# ---------------------------------------------------------------- 路由
@bp.post("/matches")
@login_required
def create_match():
    data = body_json()
    return ok(run_match(data.get("document_id", ""), data, current_user()))


@bp.get("/matches/<session_id>")
@login_required
def get_match(session_id: str):
    return ok(load_session(session_id, current_user()))
