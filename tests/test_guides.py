"""校园事务指南（R12）测试。

这一层最容易出的不是"接口 500"，而是**内容悄悄错了**：

- 版本选错：库里同时有 2025 与 2026 两版，指南指向了旧的；
- 章节串了：要「第四条」却把整章（含第五条）都端出来；
- 空分类装成有内容：知识库里根本没有对应政策，页面却渲染出一张卡片。

所以断言都打在**具体文本**与**具体 id** 上，而不是"接口返回 200"。
"""

from __future__ import annotations

import pytest

from app.contract import new_ulid
from app.models import Document, DocumentChunk, PolicyRule, db

SCHOLARSHIP = "本科生奖学金评审办法"
SERVICE_GUIDE = "学生事务办理指南"

# 两版内容刻意不同：断言"取到的是 2026 版"，只要看这句话在不在就够了
CHUNKS_2026 = [
    ("第二章 申请条件",
     "第二章 申请条件\n第四条 申请国家励志奖学金，应当同时具备下列条件：\n"
     "（一）学年平均学分绩点不低于 3.0；\n（二）经认定为家庭经济困难。\n"
     "第五条 申请校级优秀学生奖学金，应当具备下列条件：\n（一）学年平均学分绩点不低于 3.5。\n"
     "第 2 页 / 共 4", 2),
    ("第三章 评审程序与时间安排",
     "第三章 评审程序与时间安排\n第六条 奖学金评审按下列程序进行：\n"
     "（一）学生本人提出申请；\n（二）学院初审并公示 3 个工作日。\n"
     "第七条 申请材料提交截止时间为当年 9 月 10 日 17:00，逾期不再受理。\n第 3 页 / 共 4", 3),
    ("第四章 申请材料与附则",
     "第四章 申请材料与附则\n第九条 申请国家励志奖学金应当提交下列材料：\n"
     "（一）申请表（一式两份）；\n（二）上一学年成绩单。\n第 4 页 / 共 4", 4),
]

CHUNKS_2025 = [
    ("第二章 申请条件",
     "第二章 申请条件\n第四条 2025 年版旧条款：学年平均学分绩点不低于 2.8。\n第 2 页 / 共 4", 2),
]

CHUNKS_SERVICE = [
    ("二、办理流程",
     "二、办理流程\n所有事项均通过线上服务大厅提交，线下窗口仅受理特殊情况。\n"
     "登录服务大厅并完成实名认证\n选择事项类型并填写表单\n第 1 页 / 共 2", 1),
    ("三、材料要求",
     "三、材料要求\n材料不齐是最常见的退回原因，提交前请逐项核对。\n"
     "身份证件仅需核验，不留存复印件\n成绩单须为教务系统导出并带验证码\n第 1 页 / 共 2", 1),
    ("五、高频事项办理指引",
     "五、高频事项办理指引\n以下四类事项占全部受理量的八成，材料与入口各不相同。\n"
     "在读证明：服务大厅自助机打印，需本人校园卡；\n"
     "成绩单：教务系统导出并带验证码，自助机即时打印，每学期免费 3 份；\n"
     "请假与销假：3 天以内由辅导员审批，3 天以上须附校医院证明。\n第 2 页 / 共 2", 2),
    ("七、常见问题",
     "七、常见问题\n这几类问题占了咨询量的一半，先自查可以少跑一趟。\n"
     "材料不齐是最常见的退回原因，提交前请按清单逐项核对；\n"
     "委托他人办理须提供授权委托书与双方身份证件复印件；\n第 2 页 / 共 2", 2),
]


def _add_document(app, title, chunks, *, status, academic_year=None):
    with app.app_context():
        uploader = db.session.execute(
            db.select(db.func.min(Document.uploader_id))
        ).scalar()
        if uploader is None:
            from app.models import User
            uploader = db.session.execute(db.select(User.id)).scalar()

        document = Document(
            id=new_ulid(), title=title, file_name="guide.pdf", stored_name=f"{new_ulid()}.pdf",
            file_type="pdf", file_size=1024, parse_status="indexed",
            chunk_count=len(chunks), uploader_id=uploader,
            effective_status=status, academic_year=academic_year,
        )
        db.session.add(document)
        db.session.flush()
        for index, (section, content, page) in enumerate(chunks):
            db.session.add(DocumentChunk(
                id=new_ulid(), document_id=document.id, chunk_index=index,
                content=content, char_start=index * 200, char_end=(index + 1) * 200,
                page_number=page, section_title=section,
            ))
        db.session.commit()
        return document.id


@pytest.fixture()
def guide_library(app):
    """两版奖学金（2026 有效 / 2025 被替代）+ 一份事务指南 + 一组已复核规则。

    **不给图书馆、就业类文件**：这两类在演示库里也只有升学方向，
    留空正好用来验"缺资料时不许编"。贷款文件只放一份 superseded，验状态标注。
    """
    new_id = _add_document(app, f"{SCHOLARSHIP}（2026年版）", CHUNKS_2026,
                           status="active", academic_year="2026-2027")
    old_id = _add_document(app, f"{SCHOLARSHIP}（2025年版）", CHUNKS_2025,
                           status="superseded", academic_year="2025-2026")
    service_id = _add_document(app, SERVICE_GUIDE, CHUNKS_SERVICE, status="unknown")
    loan_id = _add_document(app, "国家助学贷款与勤工助学管理办法（2024年版）",
                            CHUNKS_2025, status="superseded", academic_year="2024-2025")

    with app.app_context():
        chunk_id = db.session.execute(
            db.select(DocumentChunk.id).where(DocumentChunk.document_id == new_id)
        ).scalars().first()
        rules = [
            ("R-GPA", "eligibility", "gpa", "gte", "3.0", "学年平均学分绩点不低于 3.0"),
            ("R-POVERTY", "eligibility", "poverty_certified", "equals", "yes", "经认定为家庭经济困难"),
            ("R-MATERIAL", "material", None, None, None, "提交申请表与成绩单"),
            ("R-DEADLINE", "deadline", None, None, "2026-09-10", "9 月 10 日 17:00 截止"),
        ]
        for code, rule_type, field, operator, expected, description in rules:
            db.session.add(PolicyRule(
                id=new_ulid(), document_id=new_id, chunk_id=chunk_id,
                rule_code=code, rule_type=rule_type, description=description,
                field_key=field, operator=operator, expected_value=expected,
                original_quote=description, quote_verified=1,
                extraction_status="suggested", review_status="approved",
                confidence="high", page_or_section="第二章 申请条件",
            ))
        db.session.commit()

    return {
        "new_id": new_id,
        "old_id": old_id,
        "service_id": service_id,
        "loan_id": loan_id,
    }


def get_json(client, headers, path):
    response = client.get(path, headers=headers)
    return response, response.get_json()


# ============================================================ 分类与统计
def test_overview_has_eight_categories_in_fixed_order(client, student_headers, guide_library):
    _, body = get_json(client, student_headers, "/api/v1/guides")
    assert body["code"] == 0
    data = body["data"]

    ids = [category["id"] for category in data["categories"]]
    assert ids == [
        "scholarship", "honors", "exams", "academic",
        "campus-life", "library", "career", "service",
    ]
    assert data["stats"]["category_count"] == 8
    # 统计口径必须与分类行自洽：总数 = 各分类编目条数之和
    assert data["stats"]["item_count"] == sum(
        category["item_count"] for category in data["categories"]
    )
    assert len(data["recommended"]) <= 8


def test_category_counts_separate_catalog_from_available(client, student_headers, guide_library):
    """编目条数 ≠ 有政策依据的条数。两者混为一谈，空状态就没有解释。"""
    _, body = get_json(client, student_headers, "/api/v1/guides")
    categories = {row["id"]: row for row in body["data"]["categories"]}

    scholarship = categories["scholarship"]
    assert scholarship["item_count"] == 5
    # 只有奖学金与贷款两份文件在库里：贷款只有 4 个事项共用一份文件
    assert scholarship["available_count"] >= 2
    assert scholarship["empty"] is False

    career = categories["career"]
    assert career["item_count"] == 3
    assert career["available_count"] == 0
    assert career["empty"] is True


# ============================================================ 版本选择
def test_active_version_wins_over_superseded(client, student_headers, guide_library):
    _, body = get_json(client, student_headers, "/api/v1/guides/items/inspiring-scholarship")
    data = body["data"]

    assert data["document"]["id"] == guide_library["new_id"]
    assert data["document"]["effective_status"] == "active"
    assert data["document"]["academic_year"] == "2026-2027"
    # 2025 版的条款一个字都不该出现
    assert "2.8" not in " ".join(block["text"] for block in data["blocks"])


def test_superseded_document_still_reported_with_status(client, student_headers, guide_library):
    """只剩旧版时不静默隐藏：标注「已被替代」，用户才知道要看新文件。"""
    _, body = get_json(client, student_headers, "/api/v1/guides/items/origin-student-loan")
    data = body["data"]

    assert data["availability"] == "ok"
    assert data["document"]["effective_status"] == "superseded"


# ============================================================ 空状态
def test_missing_policy_is_marked_not_invented(client, student_headers, guide_library):
    """知识库没有图书馆文件时，事项必须自报"暂未收录"，且不带任何正文。"""
    _, body = get_json(client, student_headers, "/api/v1/guides/items/book-borrow")
    data = body["data"]

    assert data["availability"] == "missing_policy"
    assert data["availability_label"] == "暂未收录相关政策"
    assert data["blocks"] == []
    assert data["faq"] == []
    assert data["document"] is None
    # 动作也不能假装可用
    assert data["actions"]["match"]["available"] is False
    assert data["actions"]["plan"]["available"] is False


# ============================================================ 详情正文
def test_detail_slices_requested_article(client, student_headers, guide_library):
    """「国家励志奖学金」只要第四条，第五条（优秀学生奖学金）不能混进来。"""
    _, body = get_json(client, student_headers, "/api/v1/guides/items/inspiring-scholarship")
    conditions = next(b for b in body["data"]["blocks"] if b["field"] == "conditions")

    assert conditions["article"] == "第四条"
    assert "第四条" in conditions["text"]
    assert "国家励志奖学金" in conditions["text"]
    assert "第五条" not in conditions["text"]
    assert conditions["page"] == 2


def test_detail_text_is_verbatim_and_page_markers_removed(client, student_headers, guide_library):
    _, body = get_json(client, student_headers, "/api/v1/guides/items/inspiring-scholarship")
    blocks = {block["field"]: block for block in body["data"]["blocks"]}

    assert "（一）申请表（一式两份）；" in blocks["materials"]["text"]
    assert "第七条 申请材料提交截止时间为当年 9 月 10 日 17:00，逾期不再受理。" \
        in blocks["steps"]["text"]
    for block in blocks.values():
        assert "第 2 页" not in block["text"]
        assert "共 4" not in block["text"]


def test_service_item_keeps_only_its_own_line(client, student_headers, guide_library):
    """「高频事项办理指引」一章写四件事，请假卡片只取请假那一行。"""
    _, body = get_json(client, student_headers,
                       "/api/v1/guides/items/leave-request")
    steps = next(b for b in body["data"]["blocks"] if b["field"] == "steps")

    assert "请假与销假" in steps["text"]
    assert "在读证明" not in steps["text"]
    assert "成绩单" not in steps["text"]
    # 导语不是步骤
    assert "占全部受理量的八成" not in steps["text"]


def test_audience_comes_from_policy_sentence(client, student_headers, app):
    """适用对象取原文里含"适用/面向"的那句话，不从标题猜。"""
    _add_document(
        app, "图书馆借阅与自习空间管理办法（2026年版）",
        [("第一章 开放时间",
          "第一章 开放时间\n第一条 本办法适用于本校全日制在校学生与在职教职工。\n"
          "第二条 读者凭校园卡刷卡入馆。\n第 1 页 / 共 1", 1)],
        status="active",
    )
    _, body = get_json(client, student_headers, "/api/v1/guides/items/book-borrow")
    audience = body["data"]["audience"]

    assert audience["source"] == "policy"
    assert "适用于本校全日制在校学生" in audience["text"]
    # 条文号不进展示文本
    assert not audience["text"].startswith("第一条")


def test_faq_drops_lead_in_sentence(client, student_headers, guide_library):
    _, body = get_json(client, student_headers, "/api/v1/guides/items/leave-request")
    faq = body["data"]["faq"]

    assert faq, "事务指南有常见问题章，不应为空"
    assert all("先自查可以少跑一趟" not in item for item in faq)
    assert any("授权委托书" in item for item in faq)


# ============================================================ 动作可用性
def test_actions_reflect_approved_rules(client, student_headers, guide_library):
    _, body = get_json(client, student_headers, "/api/v1/guides/items/inspiring-scholarship")
    actions = body["data"]["actions"]

    assert actions["ask"]["question"].startswith("国家励志奖学金申请")
    assert actions["match"]["available"] is True
    assert actions["match"]["document_id"] == guide_library["new_id"]
    assert actions["match"]["approved_condition_rules"] == 2
    assert actions["plan"]["available"] is True


def test_actions_unavailable_without_rules(client, student_headers, guide_library):
    """事务指南没有规则：能看原文，但不能跳匹配，也不该让用户跳过去撞 2008。"""
    _, body = get_json(client, student_headers, "/api/v1/guides/items/leave-request")
    actions = body["data"]["actions"]

    assert actions["match"]["available"] is False
    assert "已复核" in actions["match"]["reason"]
    assert actions["plan"]["available"] is False


# ============================================================ 列表与搜索
def test_list_filters_by_category(client, student_headers, guide_library):
    _, body = get_json(client, student_headers, "/api/v1/guides/items?category=exams")
    data = body["data"]

    assert data["total"] >= 1
    assert all(item["category"] == "exams" for item in data["items"])
    assert data["category"]["label"] == "考试与等级考试"


def test_list_searches_title_summary_and_document_title(client, student_headers, guide_library):
    _, body = get_json(client, student_headers, "/api/v1/guides/items?q=四六级")
    titles = [item["title"] for item in body["data"]["items"]]
    assert "全国大学英语四六级考试报名" in titles

    # 搜文件标题也能命中——用户常记得"那份奖学金办法"
    _, body = get_json(client, student_headers, "/api/v1/guides/items?q=奖学金评审办法")
    assert body["data"]["total"] >= 2

    _, body = get_json(client, student_headers, "/api/v1/guides/items?q=不存在的关键词")
    assert body["data"]["total"] == 0


def test_unknown_category_is_rejected(client, student_headers, guide_library):
    response = client.get("/api/v1/guides/items?category=no-such", headers=student_headers)
    assert response.status_code == 400
    assert response.get_json()["code"] == 1001


# ============================================================ 错误与鉴权
def test_unknown_item_returns_2011(client, student_headers, guide_library):
    response = client.get("/api/v1/guides/items/no-such-item", headers=student_headers)
    assert response.status_code == 404
    assert response.get_json()["code"] == 2011


def test_guides_require_login(client):
    for path in ("/api/v1/guides", "/api/v1/guides/items", "/api/v1/guides/items/cet"):
        response = client.get(path)
        assert response.status_code == 401, path


# ============================================================ 与既有能力的闭环
def test_guide_document_can_generate_plan_draft(client, student_headers, guide_library):
    """「加入事务计划」用的是既有 /plans/draft，不另写一套生成逻辑。"""
    _, detail = get_json(client, student_headers, "/api/v1/guides/items/inspiring-scholarship")
    document_id = detail["data"]["actions"]["plan"]["document_id"]

    response = client.post("/api/v1/plans/draft", headers=student_headers,
                           json={"document_id": document_id})
    body = response.get_json()
    assert body["code"] == 0
    assert body["data"]["plan_id"]
    assert body["data"]["tasks"], "有 material/deadline 规则时应能生成任务"
    assert body["data"]["plan_status"] == "draft"
