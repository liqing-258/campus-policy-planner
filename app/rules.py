"""规则抽取与复核（M5 上半）。

这条链路的核心不是"模型能不能抽出规则"，而是**模型抽错时系统守不守得住**。
模型常见的四种错法，各自对应一道校验（都在 `_validate_candidates` 里）：

| 模型输出 | 校验 | 处理 |
| --- | --- | --- |
| `original_quote` 原文里没有 | 归一化后做子串匹配 | 丢弃该条，理由写明"原文引用不在该片段中" |
| `operator` 自己发明（如 `approximately`） | 算子白名单 | 丢弃该条 |
| `field_key` 采集白名单外的字段（如 `family_income`） | 字段白名单 | 丢弃该条 |
| `chunk_index` 越界 | 1..N 整数比较 | 丢弃该条 |

丢弃而不是整批失败：一条规则脏不该让整篇抽取白跑，但**丢弃必须可见**——
响应的 `dropped[]` 里带着理由，管理员才知道"模型这次漏了什么"。

抽取结果一律 `needs_review`。这不是保守，是责任边界：资格判断会直接影响学生
能不能申请到奖助学金，未经人复核的模型输出不能进匹配。
"""

from __future__ import annotations

import json
import re

from flask import Blueprint, request

from app import llm
from app.auth import admin_required, current_user, login_required
from app.contract import body_json, ok, page_args, paginated, new_ulid
from app.documents import _get_active_or_404
from app.errors import (
    DegradedMode,
    InvalidParam,
    LlmOutputInvalid,
    RuleNotFound,
    SchemaInvalid,
)
from app.models import DocumentChunk, PolicyRule, db
from app.runtime import current_settings
from app.schemas import ExtractRequest, RuleExtractionPayload, RulePatch, validate
from shared.enums import (
    CONDITION_RULE_TYPES,
    EXTRACT_SUGGESTED,
    EXTRACT_VERIFIED,
    OP_MANUAL_REVIEW,
    REVIEW_APPROVED,
    REVIEW_NEEDS,
    REVIEW_REJECTED,
    RULE_FIELD_KEYS,
    RULE_OPERATORS,
    RULE_TYPES,
)

bp = Blueprint("rules", __name__)

SYSTEM_PROMPT = """你是高校学生事务政策的结构化抽取器。

任务：从给定的政策原文片段中抽出**可执行**的规则条目，输出 JSON。

铁律：
1. 只依据给你的原文，禁止补充原文没有的条件、金额、日期。
2. 每条规则必须给出 original_quote，它必须是原文中**连续出现**的一段文字。
   不许改写、不许拼接、不许概括——系统会拿它做子串校验，对不上就整条丢弃。
3. 原文是"表现突出""酌情考虑""综合评定"这类主观表述时，operator 填 manual_review，
   不要试图把它翻译成数值比较。
4. 拿不准就不抽。漏抽一条规则，代价远小于编一条不存在的条件。
5. chunk_index 用你看到的片段编号（从 1 开始），不要自己编号。

输出格式（只输出 JSON，不要任何解释文字）：
{"rules": [{
  "chunk_index": 1,
  "rule_code": "R-1",
  "rule_type": "eligibility|material|deadline|process|restriction",
  "description": "一句话说明这条规则要求什么",
  "field_key": "学生画像字段名，条件类规则必填，否则 null",
  "operator": "equals|not_equals|in|gte|lte|exists|contains_any|manual_review",
  "expected_value": "阈值或取值，material/deadline/process 类可为 null",
  "target_group": "适用人群，不知道就 null",
  "required_information": "需要提交的材料，没有就 null",
  "effective_period": "适用学期/学年，没有就 null",
  "page_or_section": "页码或章节",
  "original_quote": "原文中连续的一段文字",
  "confidence": "high|medium|low"
}]}

可用的 field_key（**只能**用这些，其它一律不要抽）：
student_type, grade, gpa, rank_percent, disciplinary_record,
poverty_certified, major, credits_earned, enrollment_status
"""


def build_user_prompt(chunks: list[DocumentChunk], max_rules_per_chunk: int) -> str:
    blocks = []
    for index, chunk in enumerate(chunks, 1):
        head = f"[片段 {index}] 第 {chunk.page_number or '?'} 页"
        if chunk.section_title:
            head += f" · {chunk.section_title}"
        blocks.append(f"{head}\n{chunk.content}")
    return (
        "以下是政策原文片段：\n\n"
        + "\n\n".join(blocks)
        + f"\n\n每个片段最多抽 {max_rules_per_chunk} 条规则。"
        + "没有可执行规则的片段就不要给它编规则。"
    )


# ---------------------------------------------------------------- 校验
_WS = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """压掉所有空白再比较。

    模型抄原文时常把换行写成空格、把全角标点写成半角。逐字符比较会因为一个空格
    丢掉一条正确的规则，那是把"格式差异"误判成"编造"。
    """
    return _WS.sub("", text or "")


def _quote_verified(quote: str, content: str) -> bool:
    needle = _normalize(quote)
    return bool(needle) and needle in _normalize(content)


def _validate_candidates(candidates, chunks: list[DocumentChunk], limit: int):
    """逐条校验，返回 (通过的 (candidate, chunk), 被丢弃的说明)。"""
    passed = []
    dropped = []
    per_chunk: dict[int, int] = {}

    for candidate in candidates:
        position = candidate.chunk_index
        if position < 1 or position > len(chunks):
            dropped.append({
                "rule_code": candidate.rule_code,
                "reason": f"片段编号 {position} 越界（本次共 {len(chunks)} 个片段）",
            })
            continue
        if per_chunk.get(position, 0) >= limit:
            dropped.append({
                "rule_code": candidate.rule_code,
                "reason": f"片段 {position} 的规则数超过上限 {limit}",
            })
            continue
        if candidate.rule_type not in RULE_TYPES:
            dropped.append({
                "rule_code": candidate.rule_code,
                "reason": f"规则类型 {candidate.rule_type} 不在白名单 {'/'.join(RULE_TYPES)} 中",
            })
            continue
        if candidate.operator is not None and candidate.operator not in RULE_OPERATORS:
            dropped.append({
                "rule_code": candidate.rule_code,
                "reason": f"算子 {candidate.operator} 不在白名单 {'/'.join(RULE_OPERATORS)} 中",
            })
            continue
        if candidate.field_key is not None and candidate.field_key not in RULE_FIELD_KEYS:
            dropped.append({
                "rule_code": candidate.rule_code,
                # 这条最危险：白名单外字段意味着"多采集了一份个人信息"
                "reason": f"画像字段 {candidate.field_key} 不在白名单中，拒绝入库",
            })
            continue

        chunk = chunks[position - 1]
        if not _quote_verified(candidate.original_quote, chunk.content):
            dropped.append({
                "rule_code": candidate.rule_code,
                "reason": "原文引用不在该片段中（模型可能改写了原文）",
            })
            continue

        per_chunk[position] = per_chunk.get(position, 0) + 1
        passed.append((candidate, chunk))

    return passed, dropped


# ---------------------------------------------------------------- 服务
def extract(document_id: str, payload: dict) -> dict:
    data = validate(ExtractRequest, payload)
    document = _get_active_or_404(document_id)

    chunks = db.session.execute(
        db.select(DocumentChunk)
        .where(DocumentChunk.document_id == document.id)
        .order_by(DocumentChunk.chunk_index)
    ).scalars().all()
    if data.chunk_ids:
        wanted = set(data.chunk_ids)
        chunks = [c for c in chunks if c.id in wanted]
    if not chunks:
        raise InvalidParam("该文档没有可抽取的片段；请先确认解析状态为 indexed")

    if not llm.is_configured():
        raise DegradedMode(
            "当前为降级模式：未接入模型，无法抽取规则",
            payload={"document_id": document.id, "mode": "degraded",
                     "note": "规则抽取必须调模型；请先配置 LLM_API_KEY 与 LLM_MODEL"},
        )

    # 抽取是长输出任务：一次要吐 8–30 条规则，而推理模型会先把 reasoning 写进
    # 同一个 max_tokens 预算。2026-11-10 实测：奖学金办法（4 片段）在默认 2500 下
    # reasoning_tokens=2500、content 为空，直接 4002；事务指南（6 片段）7.2 秒就过了。
    # 所以抽取单独要一份更大的预算，而不是把全局 LLM_MAX_TOKENS 一直调大——
    # 问答场景 2500 是合适的，全局调大会让每次问答都为长输出付费。
    parsed, result = llm.chat_json(
        SYSTEM_PROMPT, build_user_prompt(chunks, data.max_rules_per_chunk),
        max_tokens=max(current_settings().llm_max_tokens, 6000),
    )
    try:
        extraction = RuleExtractionPayload.model_validate(parsed)
    except Exception as exc:
        detail = str(exc).splitlines()[0][:160]
        raise LlmOutputInvalid(f"模型返回的规则结构不符合约定：{detail}")

    passed, dropped = _validate_candidates(
        extraction.rules, chunks, data.max_rules_per_chunk
    )

    existing = set(db.session.execute(
        db.select(PolicyRule.rule_code).where(PolicyRule.document_id == document.id)
    ).scalars().all())

    stored = []
    for candidate, chunk in passed:
        code = candidate.rule_code
        if code in existing:
            # 同一次抽取里模型重复给同一个 code 不罕见；加后缀而不是覆盖，
            # 否则"第二条覆盖第一条"会让抽取数量看起来正常而内容丢了一半
            suffix = 2
            while f"{code}-{suffix}" in existing:
                suffix += 1
            code = f"{code}-{suffix}"
        existing.add(code)

        rule = PolicyRule(
            id=new_ulid(),
            document_id=document.id,
            chunk_id=chunk.id,
            rule_code=code,
            rule_type=candidate.rule_type,
            description=candidate.description,
            target_group=candidate.target_group,
            field_key=candidate.field_key,
            operator=candidate.operator,
            expected_value=candidate.expected_value,
            required_information=candidate.required_information,
            effective_period=candidate.effective_period,
            page_or_section=candidate.page_or_section or _locate(chunk),
            original_quote=candidate.original_quote,
            quote_verified=1,
            extraction_status=EXTRACT_SUGGESTED,
            review_status=REVIEW_NEEDS,
            confidence=candidate.confidence,
        )
        db.session.add(rule)
        stored.append(rule)

    db.session.commit()

    return {
        "document_id": document.id,
        "mode": "llm",
        "model": result.model,
        "chunks": len(chunks),
        "extracted": len(extraction.rules),
        "stored": len(stored),
        "dropped": dropped,
        "rules": [_serialize(rule) for rule in stored],
    }


def _locate(chunk: DocumentChunk) -> str | None:
    parts = []
    if chunk.section_title:
        parts.append(chunk.section_title)
    if chunk.page_number:
        parts.append(f"第 {chunk.page_number} 页")
    return " · ".join(parts) or None


def _serialize(rule: PolicyRule) -> dict:
    return {
        "id": rule.id,
        "document_id": rule.document_id,
        "chunk_id": rule.chunk_id,
        "rule_code": rule.rule_code,
        "rule_type": rule.rule_type,
        "description": rule.description,
        "target_group": rule.target_group,
        "field_key": rule.field_key,
        "operator": rule.operator,
        "expected_value": rule.expected_value,
        "required_information": rule.required_information,
        "effective_period": rule.effective_period,
        "page_or_section": rule.page_or_section,
        "original_quote": rule.original_quote,
        "quote_verified": bool(rule.quote_verified),
        "extraction_status": rule.extraction_status,
        "review_status": rule.review_status,
        "confidence": rule.confidence,
        "created_at": rule.created_at.isoformat() + "Z" if rule.created_at else None,
    }


def _approved_rules(document_id: str) -> list[PolicyRule]:
    return db.session.execute(
        db.select(PolicyRule)
        .where(PolicyRule.document_id == document_id,
               PolicyRule.review_status == REVIEW_APPROVED)
        .order_by(PolicyRule.rule_type, PolicyRule.rule_code)
    ).scalars().all()


def update_rule(rule_id: str, payload: dict) -> dict:
    rule = db.session.get(PolicyRule, rule_id)
    if rule is None:
        raise RuleNotFound()
    data = validate(RulePatch, payload)
    changes = data.model_dump(exclude_unset=True)
    if not changes:
        raise InvalidParam("至少需要一个待修改字段")

    for key, value in changes.items():
        setattr(rule, key, value)

    # 复核通过的前提是这条规则**可以被求值**。放一条没有算子的条件规则进匹配，
    # 结果就是用户看到"待人工核实"却又不知道为什么，等于没复核。
    # 只对条件类（eligibility/restriction）要求算子：material / deadline / process
    # 本来就是"要做的事"，给它们硬塞一个算子只会逼管理员填垃圾值。
    if rule.review_status == REVIEW_APPROVED and rule.rule_type in CONDITION_RULE_TYPES:
        if not rule.operator:
            raise SchemaInvalid("复核通过前必须补齐算子：没有算子的条件规则无法参与匹配")
        if rule.operator != OP_MANUAL_REVIEW and not rule.expected_value:
            raise SchemaInvalid("复核通过前必须补齐预期值：该算子需要与阈值比较")

    if rule.review_status == REVIEW_APPROVED and rule.extraction_status == EXTRACT_SUGGESTED:
        rule.extraction_status = EXTRACT_VERIFIED

    db.session.commit()
    return _serialize(rule)


# ---------------------------------------------------------------- 路由
@bp.post("/documents/<document_id>/rules/extract")
@admin_required
def extract_route(document_id: str):
    return ok(extract(document_id, body_json()))


@bp.get("/documents/<document_id>/rules")
@login_required
def list_rules(document_id: str):
    document = _get_active_or_404(document_id)
    page, page_size = page_args()

    stmt = db.select(PolicyRule).where(PolicyRule.document_id == document.id)
    review_status = request.args.get("review_status")
    if review_status:
        if review_status not in (REVIEW_NEEDS, REVIEW_APPROVED, REVIEW_REJECTED):
            raise InvalidParam("review_status 只能是 needs_review/approved/rejected")
        stmt = stmt.where(PolicyRule.review_status == review_status)

    total = db.session.execute(
        db.select(db.func.count()).select_from(stmt.subquery())
    ).scalar() or 0
    rows = db.session.execute(
        stmt.order_by(PolicyRule.rule_type, PolicyRule.rule_code)
        .offset((page - 1) * page_size).limit(page_size)
    ).scalars().all()
    return ok(paginated([_serialize(r) for r in rows], total, page, page_size))


@bp.patch("/rules/<rule_id>")
@admin_required
def patch_rule(rule_id: str):
    return ok(update_rule(rule_id, body_json()))
