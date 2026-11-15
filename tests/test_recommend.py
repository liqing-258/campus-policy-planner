"""智能政策推荐（R13）测试。

这一层最容易出的不是 500，而是**推荐得很有道理但是错的**：

- 把未复核的规则当依据，给学生一个"符合"的假结论；
- 画像缺字段时判成"不符合"（把"没填绩点"说成"绩点不够"）；
- 推荐理由引用的原文片段根本不是那份文件里的；
- 换了画像，推荐结果一模一样的十张卡片——那就是写死的。

所以断言打在**具体结论、具体文案、具体 chunk_id** 上，而不是"接口 200"。
"""

from __future__ import annotations

import json

import pytest

from app.contract import new_ulid
from app.models import Document, DocumentChunk, MatchSession, PolicyRule, User, db

SCHOLARSHIP = "本科生奖学金评审办法"
LOAN = "国家助学贷款与勤工助学管理办法"
PUSH = "推荐免试攻读研究生遴选办法"

CONDITION = "第二章 申请条件"
LOAN_ONE = "第一章 生源地信用助学贷款"
WORK = "第三章 勤工助学"
HARDSHIP = "第五章 家庭经济困难学生认定"

# 两版内容刻意不同：断言"取到的是现行版"，看这句话在不在就够了
CHUNK_GENERAL_2026 = (
    "第一章 总则",
    "第一章 总则\n第二条 本办法适用于具有本校正式学籍的全日制在校本科学生。\n"
    "第 1 页 / 共 4",
    1,
)
CHUNK_CONDITION_2026 = (
    CONDITION,
    "第二章 申请条件\n第四条 申请国家励志奖学金，应当同时具备下列条件：\n"
    "（一）为全日制在校本科二年级以上（含二年级）学生；\n"
    "（二）学年平均学分绩点不低于 3.0，且专业排名位于前 30%；\n"
    "（三）经认定为家庭经济困难；\n"
    "（四）学年内无纪律处分记录。\n"
    "第五条 申请校级优秀学生奖学金，应当具备下列条件：\n"
    "（一）学年平均学分绩点不低于 3.5；\n"
    "（二）综合素质测评成绩位于本专业前 15%。\n"
    "第 2 页 / 共 4",
    2,
)
CHUNK_MATERIAL_2026 = (
    "第四章 申请材料与附则",
    "第四章 申请材料与附则\n第九条 申请国家励志奖学金应当提交下列材料：\n"
    "（一）申请表（一式两份）；\n（二）上一学年成绩单。\n第 4 页 / 共 4",
    4,
)
CHUNK_GENERAL_2025 = (
    "第一章 总则",
    "第一章 总则\n第二条 2025 年版旧条款：本办法适用于全日制在校本科学生。\n"
    "第 1 页 / 共 4",
    1,
)

CHUNK_LOAN_ONE = (
    LOAN_ONE,
    "第一章 生源地信用助学贷款\n"
    "第一条 家庭经济困难的全日制本专科学生可以申请生源地信用助学贷款，"
    "每人每年最高 16000 元。\n"
    "第二条 贷款学生在校期间的利息由财政全额贴息。\n"
    "第三条 每年 7 月至 9 月为生源地贷款集中办理期，学生凭录取通知书或学生证"
    "到户籍所在地县级学生资助管理中心办理。\n"
    "第 1 页 / 共 5",
    1,
)
CHUNK_WORK = (
    WORK,
    "第三章 勤工助学\n第五条 学校设置校内勤工助学岗位，按小时计酬。\n"
    "第七条 勤工助学岗位每学期初集中发布，优先安排家庭经济困难学生。\n"
    "第 2 页 / 共 5",
    2,
)
# 勤工助学"要准备什么"在第七章，不在第三章：条件摘要不能拿第七条顶上
CHUNK_WORK_FAQ = (
    "第七章 常见问题",
    "第七章 常见问题\n第十五条 勤工助学上岗前须完成 2 学时岗前培训，考核合格后安排岗位；"
    "每月 25 日前提交工时表。\n第 4 页 / 共 5",
    4,
)
CHUNK_HARDSHIP = (
    HARDSHIP,
    "第五章 家庭经济困难学生认定\n"
    "第九条 认定结果分为特别困难、困难、一般困难三档，每学年 9 月集中认定一次。\n"
    "第十条 认定材料包括家庭经济困难学生认定申请表、低保等佐证材料。\n"
    "第十一条 认定结果与奖学金评定、助学贷款办理、勤工助学岗位分配挂钩。\n"
    "第 3 页 / 共 5",
    3,
)
CHUNK_PUSH = (
    CONDITION,
    "第二章 申请条件\n第三条 申请推免应当同时具备下列条件：\n"
    "（一）纳入国家普通本科招生计划录取的应届毕业生；\n"
    "（二）前六个学期平均学分绩点不低于 3.5。\n"
    "第 2 页 / 共 5",
    2,
)


def _document(title: str, uploader_id: str, *, status: str = "unknown",
              year: str | None = None) -> Document:
    return Document(
        id=new_ulid(), title=title, file_name=f"{title}.pdf", stored_name=new_ulid(),
        file_type="pdf", file_size=1024, effective_status=status, academic_year=year,
        parse_status="indexed", chunk_count=1, uploader_id=uploader_id,
    )


def _chunks(doc: Document, rows) -> list[DocumentChunk]:
    out = []
    for index, (section, content, page) in enumerate(rows):
        out.append(DocumentChunk(
            id=new_ulid(), document_id=doc.id, chunk_index=index, content=content,
            char_start=0, char_end=len(content), page_number=page, section_title=section,
        ))
    return out


def _rule(doc, chunk, code, *, group, field, operator, expected, rule_type="eligibility",
          status="approved", description=None, quote=None) -> PolicyRule:
    return PolicyRule(
        id=new_ulid(), document_id=doc.id, chunk_id=chunk.id, rule_code=code,
        rule_type=rule_type, target_group=group, field_key=field, operator=operator,
        expected_value=expected, description=description or f"{group or '通用'}的{field}条件。",
        original_quote=quote or (description or f"{group or '通用'}的{field}条件。"),
        quote_verified=1, extraction_status="suggested", review_status=status,
    )


@pytest.fixture()
def library(app):
    """一份最小但真实的政策库：两版奖学金办法 + 助学贷款办法 + 推免办法。"""
    with app.app_context():
        admin = db.session.execute(
            db.select(User).where(User.username == "admin")
        ).scalars().first()
        old = _document(f"{SCHOLARSHIP}（2025年版）", admin.id,
                        status="superseded", year="2025-2026")
        new = _document(f"{SCHOLARSHIP}（2026年版）", admin.id,
                        status="active", year="2026-2027")
        loan = _document(LOAN, admin.id)
        push = _document(PUSH, admin.id)
        db.session.add_all([old, new, loan, push])
        db.session.flush()

        old_chunks = _chunks(old, [CHUNK_GENERAL_2025])
        new_chunks = _chunks(new, [CHUNK_GENERAL_2026, CHUNK_CONDITION_2026, CHUNK_MATERIAL_2026])
        loan_chunks = _chunks(loan, [CHUNK_LOAN_ONE, CHUNK_WORK, CHUNK_WORK_FAQ, CHUNK_HARDSHIP])
        push_chunks = _chunks(push, [CHUNK_PUSH])
        db.session.add_all(old_chunks + new_chunks + loan_chunks + push_chunks)
        db.session.flush()

        general, condition, material = new_chunks
        db.session.add_all([
            # 励志：5 条全部已复核——这是全库唯一能"完整判定"的奖项
            _rule(new, condition, "R-1", group="国家励志奖学金申请人", field="grade",
                  operator="gte", expected="2", description="申请国家励志奖学金须为二年级以上学生。"),
            _rule(new, condition, "R-2", group="国家励志奖学金申请人", field="gpa",
                  operator="gte", expected="3.0", description="学年平均学分绩点不低于 3.0。"),
            _rule(new, condition, "R-3", group="国家励志奖学金申请人", field="rank_percent",
                  operator="lte", expected="30", description="专业排名位于前 30%。"),
            _rule(new, condition, "R-4", group="国家励志奖学金申请人", field="poverty_certified",
                  operator="equals", expected="yes", description="须认定为家庭经济困难学生。"),
            _rule(new, condition, "R-5", group="国家励志奖学金申请人",
                  field="disciplinary_record", operator="equals", expected="none",
                  description="学年内无纪律处分记录。"),
            # 校级：原文有条件，但三条全被退回——推荐必须给"暂无法判断"而不是"不符合"
            _rule(new, condition, "R-6", group="校级优秀学生奖学金申请人", field="gpa",
                  operator="gte", expected="3.5", status="rejected",
                  description="学年平均学分绩点不低于 3.5。"),
            _rule(new, condition, "R-7", group="校级优秀学生奖学金申请人", field="rank_percent",
                  operator="lte", expected="15", status="rejected",
                  description="综合素质测评成绩位于本专业前 15%。"),
            _rule(new, condition, "R-8", group="校级优秀学生奖学金申请人",
                  field="disciplinary_record", operator="equals", expected="none",
                  status="rejected", description="学年内无纪律处分记录。"),
            # 通用限制：未标注奖项、被退回。它不该被算进任何一个奖项的"被退回条数"
            _rule(new, general, "R-9", group=None, field="student_type",
                  operator="not_equals", expected="交换生", rule_type="restriction",
                  status="rejected", description="交换生、旁听生不适用本办法。"),
            # 计划类规则：只有已复核的材料/截止时间才允许"生成办理计划"
            _rule(new, material, "R-10", group="国家励志奖学金申请人", field=None,
                  operator="manual_review", expected=None, rule_type="material",
                  description="须提交国家励志奖学金申请表（一式两份）。"),
        ])
        # 先把 id 取出来再提交：commit 会让实例过期，出了 app_context 再读属性
        # 就是 DetachedInstanceError（不是"没有数据"，是"没有会话"）
        ids = {"old": old.id, "new": new.id, "loan": loan.id, "push": push.id}
        db.session.commit()
    return ids


def _login(client, username="student", password="student123") -> dict:
    resp = client.post("/api/v1/auth/login",
                       json={"username": username, "password": password})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return {"Authorization": f"Bearer {resp.get_json()['data']['token']}"}


def _add_session(app, profile: dict, document_id: str | None = None) -> str:
    """落一条匹配记录——推荐页的画像唯一来源就是这张表。"""
    with app.app_context():
        user = db.session.execute(
            db.select(User).where(User.username == "student")
        ).scalars().first()
        session = MatchSession(
            id=new_ulid(), user_id=user.id,
            document_id=document_id or db.session.execute(db.select(Document.id)).scalars().first(),
            profile_json=json.dumps(profile, ensure_ascii=False),
            rule_snapshot_json="[]",
        )
        db.session.add(session)
        db.session.commit()
        return session.id


def _get(client, headers) -> dict:
    resp = client.get("/api/v1/recommendations", headers=headers)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["code"] == 0, body
    return body["data"]


def _by_key(data: dict, key: str) -> dict:
    for card in data["items"]:
        if card["key"] == key:
            return card
    raise AssertionError(f"{key} 不在推荐列表里：{[c['key'] for c in data['items']]}")


GOOD_PROFILE = {
    "student_type": "全日制在校本科生", "grade": 2, "gpa": 3.6, "rank_percent": 10.0,
    "disciplinary_record": "none", "poverty_certified": "yes",
    "major": "物联网工程", "credits_earned": 80, "enrollment_status": "enrolled",
}


# ---------------------------------------------------------------- 画像与三态
def test_no_profile_still_returns_catalog_with_unknown(app, client, library):
    """没有画像时**不返回空列表**：政策本身是真实存在的，缺的是判断依据。

    需求 4.3 允许"信息不完整时也推荐"，但必须说清缺什么——所以每张卡片都是
    暂无法判断，且 `missing_labels` 列出九项。
    """
    headers = _login(client)
    data = _get(client, headers)
    assert data["profile"]["source"] == "none"
    assert data["summary"]["total"] > 0
    assert data["summary"]["met"] == 0 and data["summary"]["not_met"] == 0
    assert "no_profile" in [n["code"] for n in data["notices"]]
    card = _by_key(data, "inspiring-scholarship")
    assert card["status"] == "unknown"
    assert set(card["missing_labels"]) >= {"平均学分绩点", "专业排名百分比"}


def test_full_profile_meets_inspiring_scholarship(app, client, library):
    _add_session(app, GOOD_PROFILE)
    data = _get(client, _login(client))
    card = _by_key(data, "inspiring-scholarship")
    assert card["status"] == "met", card["reason"]
    assert card["status_label"] == "可能符合"
    assert card["condition_counts"]["met"] == 5
    assert card["rank"] == 1, "可能符合必须排第一"
    assert "5 项已复核条件" in card["reason"]["text"]


def test_missing_field_is_unknown_not_not_met(app, client, library):
    """没填绩点 ≠ 绩点不够。这是三态存在的全部理由。"""
    profile = dict(GOOD_PROFILE)
    profile.pop("gpa")
    _add_session(app, profile)
    card = _by_key(_get(client, _login(client)), "inspiring-scholarship")
    assert card["status"] == "unknown"
    assert card["missing_labels"] == ["平均学分绩点"]
    assert "还缺 1 项信息" in card["reason"]["text"]


def test_one_explicit_violation_wins_over_missing_info(app, client, library):
    """存在明确不符合时，哪怕还有条件缺信息，也必须判"暂不符合"（需求 3.2）。"""
    profile = dict(GOOD_PROFILE)
    profile["gpa"] = 2.5
    profile.pop("rank_percent")
    _add_session(app, profile)
    card = _by_key(_get(client, _login(client)), "inspiring-scholarship")
    assert card["status"] == "not_met"
    assert card["condition_counts"]["not_met"] == 1
    # 理由里必须出现两个数字：只说"不符合"，用户不知道差多少。
    # 阈值写成"3"而不是"3.0"是 matching._trim 的既有行为（整数值不补小数），
    # 这里跟着它的口径断言，不为了这一条去改三态判定的文案。
    assert "2.5" in card["reason"]["highlights"][0]
    assert "不低于 3" in card["reason"]["highlights"][0]


def test_unreviewed_rules_never_produce_a_verdict(app, client, library):
    """校级奖学金的 3 条规则被退回：只能给"暂无法判断"，且要说清是几条。"""
    _add_session(app, GOOD_PROFILE)
    card = _by_key(_get(client, _login(client)), "excellent-scholarship")
    assert card["status"] == "unknown"
    assert card["reason"]["code"] == "rules_rejected"
    assert card["condition_counts"]["rejected"] == 3, "通用限制规则不该被算进本奖项"
    assert card["condition_counts"]["approved"] == 0
    assert "3 条条件规则未通过复核" in card["reason"]["text"]
    # 原文条件仍然要摆出来：学生有权知道文件写了什么，只是系统不据此判定
    assert "3.5" in card["conditions_preview"]


def test_subject_without_rules_points_at_the_group_that_has_them(app, client, library):
    """国家奖学金一条规则都没有，但同文件的 5 条已复核规则全在励志上——要指名。"""
    _add_session(app, GOOD_PROFILE)
    card = _by_key(_get(client, _login(client)), "national-grant")
    assert card["status"] == "unknown"
    assert card["reason"]["code"] == "rules_other_group"
    assert "国家励志奖学金" in card["reason"]["text"]
    # 不能把励志的五个条件当成国家奖学金的条件
    assert card["conditions_preview"] == ""
    assert card["conditions_available"] is False


def test_explicit_violation_requires_poverty_certification(app, client, library):
    profile = dict(GOOD_PROFILE)
    profile["poverty_certified"] = "no"
    _add_session(app, profile)
    card = _by_key(_get(client, _login(client)), "inspiring-scholarship")
    assert card["status"] == "not_met"
    failing = [row for row in card["conditions"] if row["status"] == "not_met"]
    assert [row["field_key"] for row in failing] == ["poverty_certified"]


# ---------------------------------------------------------------- 适用范围
def test_support_out_of_scope_is_excluded_with_a_reason(app, client, library):
    """非困难学生不该被推荐助学贷款——但"为什么不推"要能查。"""
    profile = dict(GOOD_PROFILE)
    profile["poverty_certified"] = "no"
    _add_session(app, profile)
    data = _get(client, _login(client))
    keys = [card["key"] for card in data["items"]]
    assert "origin-student-loan" not in keys and "work-study" not in keys
    excluded = {row["key"]: row for row in data["excluded"]}
    assert excluded["origin-student-loan"]["code"] == "out_of_scope"
    assert "未认定" in excluded["origin-student-loan"]["reason"]


def test_audience_and_conditions_are_not_the_same_sentence(app, client, library):
    """「适用对象」和「主要申请条件」不能是同一句话。

    编目里两处指向同一条时，卡片会把同一段原文印两遍，读起来像 bug。
    这里钉住三张资助/服务卡的取文位置：贷款取第三条（办理期与凭证）、
    勤工助学取第十五条（岗前培训与工时表）、推免的适用对象只取（一）那一行。
    """
    _add_session(app, dict(GOOD_PROFILE, grade=4))
    data = _get(client, _login(client))

    loan = _by_key(data, "origin-student-loan")
    assert "16000 元" in loan["audience"]["text"]
    assert "集中办理期" in loan["conditions_preview"]
    assert loan["audience"]["text"].strip() != loan["conditions_preview"]
    # 理由说的是"适用对象一致"，引的就必须是适用对象那一条
    assert "第一条" in loan["reason"]["text"]
    assert "第三条" not in loan["reason"]["text"]

    work = _by_key(data, "work-study")
    assert "优先安排家庭经济困难学生" in work["audience"]["text"]
    assert "岗前培训" in work["conditions_preview"]

    push = _by_key(data, "postgraduate-recommend")
    assert "应届毕业生" in push["audience"]["text"]
    # 适用对象只该是（一）那一行，不能把五条条件整段搬过来
    assert "六级" not in push["audience"]["text"]
    assert "前六个学期" in push["conditions_preview"]


def test_scope_changes_with_the_profile(app, client, library):
    """同一套编目，画像不同 → 进推荐的政策不同。这是"没有写死"的直接证据。"""
    poor = dict(GOOD_PROFILE)
    rich = dict(GOOD_PROFILE, poverty_certified="no")
    _add_session(app, poor)
    with_loan = [c["key"] for c in _get(client, _login(client))["items"]]
    _add_session(app, rich)
    without_loan = [c["key"] for c in _get(client, _login(client))["items"]]
    assert "origin-student-loan" in with_loan
    assert "origin-student-loan" not in without_loan
    # 困难认定反过来：没认定时才作为"前置条件缺失"重点推荐
    data = _get(client, _login(client))
    card = _by_key(data, "hardship-certification")
    assert card["reason"]["code"] == "prereq_gap"
    assert "国家励志奖学金" in card["reason"]["text"]


def test_push_only_recommended_for_graduating_undergraduates(app, client, library):
    """推免的适用对象是"应届本科毕业生"：大二不推，大四推，专科不推。"""
    _add_session(app, dict(GOOD_PROFILE, grade=2))
    keys = [c["key"] for c in _get(client, _login(client))["items"]]
    assert "postgraduate-recommend" not in keys

    _add_session(app, dict(GOOD_PROFILE, grade=4))
    keys = [c["key"] for c in _get(client, _login(client))["items"]]
    assert "postgraduate-recommend" in keys

    _add_session(app, dict(GOOD_PROFILE, student_type="全日制在校专科生", grade=3))
    data = _get(client, _login(client))
    keys = [c["key"] for c in data["items"]]
    assert "postgraduate-recommend" not in keys
    # 专科生连奖学金办法都不适用（第二条只写本科生）
    assert "inspiring-scholarship" not in keys


# ---------------------------------------------------------------- 溯源与稳定
def test_every_card_traces_back_to_a_real_chunk(app, client, library):
    _add_session(app, GOOD_PROFILE)
    data = _get(client, _login(client))
    with app.app_context():
        chunk_ids = set(db.session.execute(db.select(DocumentChunk.id)).scalars())
    checked = 0
    for card in data["items"]:
        assert card["document"] is not None
        assert card["document"]["id"]
        for row in card["conditions"]:
            assert row["evidence"]["chunk_id"] in chunk_ids
            assert row["evidence"]["quote"]
            checked += 1
        if card["evidence"]:
            assert card["evidence"]["chunk_id"] in chunk_ids
            assert card["evidence"]["text"].strip()
            checked += 1
    assert checked >= 8


def test_active_version_wins_over_superseded(app, client, library):
    """库里同时有 2025 与 2026 两版，推荐必须挂在现行版上。"""
    _add_session(app, GOOD_PROFILE)
    card = _by_key(_get(client, _login(client)), "inspiring-scholarship")
    assert "2026" in card["document"]["title"]
    assert card["document"]["effective_status"] == "active"
    assert "2025 年版旧条款" not in (card["evidence"] or {}).get("text", "")


def test_repeated_calls_are_identical(app, client, library):
    """推荐是纯函数：同一份画像两次调用，除时间戳外必须逐字节一致。"""
    _add_session(app, GOOD_PROFILE)
    headers = _login(client)
    first, second = _get(client, headers), _get(client, headers)
    assert json.dumps(first["items"], ensure_ascii=False) == json.dumps(
        second["items"], ensure_ascii=False
    )
    assert first["summary"] == second["summary"]


def test_recommendation_writes_nothing(app, client, library):
    """只读：不建匹配会话、不写计划、不动规则。"""
    _add_session(app, GOOD_PROFILE)
    with app.app_context():
        before = (
            db.session.execute(db.select(db.func.count(MatchSession.id))).scalar(),
            db.session.execute(db.select(db.func.count(PolicyRule.id))).scalar(),
        )
    _get(client, _login(client))
    with app.app_context():
        after = (
            db.session.execute(db.select(db.func.count(MatchSession.id))).scalar(),
            db.session.execute(db.select(db.func.count(PolicyRule.id))).scalar(),
        )
    assert before == after


# ---------------------------------------------------------------- 动作与覆盖
def test_plan_and_match_only_available_where_rules_exist(app, client, library):
    """「进行资格匹配」「生成办理计划」都不能把用户送进 2008 / 生成错材料的草稿。"""
    _add_session(app, GOOD_PROFILE)
    data = _get(client, _login(client))

    inspiring = _by_key(data, "inspiring-scholarship")
    assert inspiring["actions"]["match"]["available"] is True
    assert inspiring["actions"]["plan"]["available"] is True
    assert inspiring["actions"]["plan"]["document_id"] == inspiring["document"]["id"]

    excellent = _by_key(data, "excellent-scholarship")
    assert excellent["actions"]["match"]["available"] is False
    assert excellent["actions"]["match"]["reason"]
    assert excellent["actions"]["plan"]["available"] is False

    # 详情：有指南条目走指南，没有就落到知识库的那份文件
    assert inspiring["actions"]["detail"]["path"] == "/guides/inspiring-scholarship"
    assert _by_key(data, "national-grant")["actions"]["detail"]["path"].startswith("/library?doc=")


def test_coverage_names_the_policy_that_is_not_in_the_library(app, client, library):
    """需求点名的"助学金"库里没有：要写在覆盖说明里，而不是造一张卡片。"""
    _add_session(app, GOOD_PROFILE)
    data = _get(client, _login(client))
    coverage = {row["label"]: row for row in data["coverage"]}
    assert coverage["助学金"]["status"] == "missing"
    assert coverage["国家奖学金"]["status"] == "covered"
    assert data["summary"]["cataloged"] == len(data["items"]) + len(data["excluded"])


def test_missing_document_is_excluded_not_invented(app, client, library):
    """文件被下架（软删）时，这条事项只能进"未推荐"，不能留一张空卡片。"""
    with app.app_context():
        doc = db.session.execute(
            db.select(Document).where(Document.title == PUSH)
        ).scalars().first()
        doc.deleted_at = db.func.now()
        db.session.commit()
    _add_session(app, dict(GOOD_PROFILE, grade=4))
    data = _get(client, _login(client))
    assert "postgraduate-recommend" not in [c["key"] for c in data["items"]]
    excluded = {row["key"]: row for row in data["excluded"]}
    assert excluded["postgraduate-recommend"]["code"] == "missing_policy"


def test_requires_login(client):
    resp = client.get("/api/v1/recommendations")
    assert resp.status_code == 401
    assert resp.get_json()["code"] != 0
