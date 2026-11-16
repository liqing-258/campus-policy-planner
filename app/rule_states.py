"""文件级「资格匹配准备度」（R20）。

**这个模块解决什么问题**

在 R20 之前，一份政策在资格匹配页面上只有一种表达：`已复核条件 N 条`。
于是下面四种完全不同的处境在前端长得一模一样：

| 真实处境 | 之前显示 | 管理员该做什么 | 学生该看到什么 |
| --- | --- | --- | --- |
| 从没抽过规则 | 0 条 | 点「抽取规则」 | 该政策尚未配置匹配条件 |
| 抽过但失败（如预算耗尽） | 0 条 | 看失败原因、重试 | 同上，但要说明是失败而非"没条件" |
| 抽到了但还没复核 | 0 条 | 去规则管理逐条复核 | 条件待审核，暂不能匹配 |
| 本来就没有申请资格条件 | 0 条 | 标记「不适用于资格匹配」 | 该政策不涉及申请资格 |

四件事被压成一个数字，结果就是"17 份政策都显示 0 条、都能选中、选了都报 2008"。

**哪些落库，哪些推导**

- 落库（`document_rule_states`）：管理员「不适用」的判定 + 理由；上一次抽取尝试的结果。
  这两件是**人的决定**与**发生的事件**，推导不出来。
- 推导（本模块 `derive_state`）：已具备 / 待审核 / 待提取。
  它们随规则表变化，落库就会在复核之后静默过期——而它没有"过期"标记。

**状态优先级：数据压过标签**

`derive_state` 把 `ready` 排在 `not_applicable` 之前：如果有人先把文件标了"不适用"、
后来又复核通过了资格条件，匹配是**真的会跑**的，此时显示"不适用"就是骗人。
这种自相矛盾会以 `conflict=True` 单独暴露，由界面提示管理员去改标记。
"""

from __future__ import annotations

import json

from flask import current_app

from app.contract import new_ulid
from app.models import DocumentRuleState, PolicyRule, db
from app.schemas import RuleApplicabilityRequest, validate
from shared.enums import (
    APPLICABILITY_APPLICABLE,
    APPLICABILITY_NOT_APPLICABLE,
    APPLICABILITY_UNCLASSIFIED,
    ATTEMPT_FAILED,
    ATTEMPT_SUCCEEDED,
    CONDITION_RULE_TYPES,
    REVIEW_APPROVED,
    REVIEW_NEEDS,
    RULE_STATE_EXTRACT_FAILED,
    RULE_STATE_NO_CONDITIONS,
    RULE_STATE_NOT_APPLICABLE,
    RULE_STATE_PENDING_EXTRACT,
    RULE_STATE_PENDING_REVIEW,
    RULE_STATE_READY,
)

CONDITION_TYPES = tuple(CONDITION_RULE_TYPES)


def derive_state(*, approved_conditions: int, total_rules: int,
                 applicability: str, last_status: str | None) -> str:
    """由"规则表现状 + 人工判定 + 上次尝试"推出唯一状态。

    顺序是有讲究的，四条都不能换位：

    1. `ready` 先判：有已复核的资格条件就是**能匹配**，匹配接口也只认这个，
       此时说"不适用"或"待提取"都会与实际行为不符。
    2. `not_applicable` 次之：这是人工结论，没条件可匹配时以它为准。
    3. `pending_review` 再看：已经有规则（哪怕只是流程类），下一步是去复核，
       不是重新抽取——先抽取会得到重复规则。
    4. `extract_failed` 与 `pending_extract` 最后分：两者都是"没有规则"，
       区别只在**有没有留下失败痕迹**，而这个区别决定管理员是"重试"还是"开始"。
    """
    if approved_conditions > 0:
        return RULE_STATE_READY
    if applicability == APPLICABILITY_NOT_APPLICABLE:
        return RULE_STATE_NOT_APPLICABLE
    if total_rules > 0:
        return RULE_STATE_PENDING_REVIEW
    if last_status == ATTEMPT_FAILED:
        return RULE_STATE_EXTRACT_FAILED
    if last_status == ATTEMPT_SUCCEEDED:
        # 试过、成功、但一条都没抽到。与"没试过"必须分开：前者该做判断，
        # 后者该去点抽取。**它仍是待确认状态**——模型"拿不准就不抽"也在这里。
        return RULE_STATE_NO_CONDITIONS
    return RULE_STATE_PENDING_EXTRACT


def summaries_for(document_ids: list[str]) -> dict[str, dict]:
    """批量算每份文件的规则汇总。**一次**聚合查询，供 `/documents` 列表复用。

    为什么要有这个批量入口：前端 `MatchView` 早先是逐个文件去查
    `/documents/{id}/rules?review_status=approved`，18 份文件就是 18 次往返
    （而且第 19 次才拿到文档列表）。状态要跟着文档一起出现，就必须批量算。
    """
    if not document_ids:
        return {}

    condition_flag = db.case(
        (PolicyRule.rule_type.in_(CONDITION_TYPES), 1), else_=0
    )
    rows = db.session.execute(
        db.select(
            PolicyRule.document_id.label("doc_id"),
            db.func.count(PolicyRule.id).label("total"),
            db.func.sum(condition_flag).label("conditions"),
            db.func.sum(
                db.case((PolicyRule.review_status == REVIEW_APPROVED, 1), else_=0)
            ).label("approved"),
            db.func.sum(
                db.case(
                    (db.and_(PolicyRule.review_status == REVIEW_APPROVED,
                             PolicyRule.rule_type.in_(CONDITION_TYPES)), 1),
                    else_=0,
                )
            ).label("approved_conditions"),
            db.func.sum(
                db.case((PolicyRule.review_status == REVIEW_NEEDS, 1), else_=0)
            ).label("pending_review"),
        )
        .where(PolicyRule.document_id.in_(document_ids))
        .group_by(PolicyRule.document_id)
    ).all()
    stats = {r.doc_id: r for r in rows}

    states = {
        s.document_id: s
        for s in db.session.execute(
            db.select(DocumentRuleState).where(
                DocumentRuleState.document_id.in_(document_ids)
            )
        ).scalars().all()
    }

    # 申请奖项下拉的来源：已复核的**资格条件**上标注的 target_group。
    # 一并放进 summary，前端才不用为了这一个列表再逐份文件请求一次。
    group_rows = db.session.execute(
        db.select(PolicyRule.document_id, PolicyRule.target_group)
        .where(
            PolicyRule.document_id.in_(document_ids),
            PolicyRule.review_status == REVIEW_APPROVED,
            PolicyRule.rule_type.in_(CONDITION_TYPES),
            PolicyRule.target_group.isnot(None),
        )
        .distinct()
    ).all()
    groups: dict[str, list[str]] = {}
    for doc_id, group in group_rows:
        name = (group or "").strip()
        if name:
            groups.setdefault(doc_id, set()).add(name)  # type: ignore[arg-type]
    groups = {k: sorted(v) for k, v in groups.items()}  # type: ignore[assignment]

    out: dict[str, dict] = {}
    for doc_id in document_ids:
        row = stats.get(doc_id)
        total = int(row.total or 0) if row else 0
        conditions = int(row.conditions or 0) if row else 0
        approved = int(row.approved or 0) if row else 0
        approved_conditions = int(row.approved_conditions or 0) if row else 0
        pending = int(row.pending_review or 0) if row else 0
        state = states.get(doc_id)
        applicability = state.applicability if state else APPLICABILITY_UNCLASSIFIED
        last_status = state.last_attempt_status if state else None
        out[doc_id] = {
            "state": derive_state(
                approved_conditions=approved_conditions,
                total_rules=total,
                applicability=applicability,
                last_status=last_status,
            ),
            "applicability": applicability,
            "applicability_note": state.applicability_note if state else None,
            "total_rules": total,
            "condition_rules": conditions,
            "approved_rules": approved,
            "approved_conditions": approved_conditions,
            "pending_review": pending,
            # 可供选择的申请奖项（来自已复核资格条件的 target_group）
            "target_groups": groups.get(doc_id, []),
            "last_attempt_at": (
                state.last_attempt_at.isoformat() + "Z"
                if state and state.last_attempt_at else None
            ),
            "last_attempt_status": last_status,
            "last_attempt_message": state.last_attempt_message if state else None,
            "last_attempt_rules": state.last_attempt_rules if state else None,
            # 人工标了"不适用"、但后来又复核通过资格条件：匹配照样会跑，
            # 所以不能静默按"不适用"显示，得让管理员看见这个矛盾
            "conflict": bool(
                applicability == APPLICABILITY_NOT_APPLICABLE and approved_conditions > 0
            ),
        }
    return out


def summary_of(document_id: str) -> dict:
    return summaries_for([document_id]).get(document_id, {})


def get_or_create(document_id: str) -> DocumentRuleState:
    row = db.session.execute(
        db.select(DocumentRuleState).where(DocumentRuleState.document_id == document_id)
    ).scalars().first()
    if row is None:
        row = DocumentRuleState(
            id=new_ulid(), document_id=document_id,
            applicability=APPLICABILITY_UNCLASSIFIED,
        )
        db.session.add(row)
        db.session.flush()
    return row


def record_attempt(document_id: str, *, status: str, message: str | None = None,
                   rules: int | None = None) -> None:
    """记录一次抽取尝试的结果。**失败也要记**——这正是 R20 的核心缺口：
    失败不留痕，界面上"抽取失败"和"从没抽过"就完全一样。

    自身异常一律吞掉（与 `documents._audit` 同一口径）：记状态失败不该把
    一次已经成功的抽取变成 500，也不该盖掉正在往上抛的原始异常。
    """
    if status not in (ATTEMPT_SUCCEEDED, ATTEMPT_FAILED):
        raise ValueError(f"unknown attempt status: {status}")
    try:
        row = get_or_create(document_id)
        row.last_attempt_status = status
        row.last_attempt_message = (message or None)
        row.last_attempt_rules = rules
        from datetime import datetime, timezone
        row.last_attempt_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.session.commit()
    except Exception:  # pragma: no cover - 记录失败不影响主流程
        db.session.rollback()
        current_app.logger.warning("record_attempt failed: %s", document_id)


def set_applicability(document_id: str, payload: dict, user) -> dict:
    """管理员判定「这份政策是否参与资格匹配」。

    **为什么"标不适用"要先看有没有已复核条件**

    `not_applicable` 的语义是"这份文件里没有申请资格条件"。如果它已经有
    已复核的资格条件，这个标记就是**和事实冲突**的：匹配接口只按规则查，
    不会因为标了不适用就停下。所以这里直接拒绝，要求先把规则驳回——
    让矛盾在写入时就暴露，而不是留给界面去解释。
    """
    from app.documents import _audit, _get_active_or_404
    from app.errors import SchemaInvalid

    data = validate(RuleApplicabilityRequest, payload)
    document = _get_active_or_404(document_id)
    summary = summary_of(document.id)

    if data.applicability == APPLICABILITY_NOT_APPLICABLE:
        if summary["approved_conditions"] > 0:
            raise SchemaInvalid(
                f"该文件已有 {summary['approved_conditions']} 条已复核的资格条件，"
                "不能标记为「不适用于资格匹配」；请先在规则管理里驳回这些规则"
            )
        if not (data.note or "").strip():
            raise SchemaInvalid(
                "标记「不适用于资格匹配」必须填写理由："
                "下一个人看到这份文件时，需要知道你依据什么做出这个判断"
            )

    row = get_or_create(document.id)
    row.applicability = data.applicability
    row.applicability_note = (data.note or "").strip() or None
    from datetime import datetime, timezone
    row.decided_by = user.id
    row.decided_at = datetime.now(timezone.utc).replace(tzinfo=None)

    # 这是"要不要让这份政策参与资格判断"的判定，属于必须留痕的写操作。
    # **必须在 commit 之前调**：`_audit` 只 add 不 commit（它的调用方都自己提交），
    # 写在 commit 之后就等于加进了一个再也不会被提交的会话——R20 的测试抓到过这一版。
    _audit("document.rule-applicability", "document", document.id,
           json.dumps({"applicability": data.applicability,
                       "note": row.applicability_note}, ensure_ascii=False))
    db.session.commit()

    refreshed = summary_of(document.id)
    hint = None
    if data.applicability == APPLICABILITY_APPLICABLE and not refreshed["approved_conditions"]:
        # 允许，但要说清它现在还不能匹配——否则管理员以为点完就好了
        hint = ("已标记为适用，但当前还没有已复核的资格条件；"
                "学生此时仍无法匹配，请先抽取并复核规则")
    return {"document_id": document.id, "summary": refreshed, "hint": hint}
