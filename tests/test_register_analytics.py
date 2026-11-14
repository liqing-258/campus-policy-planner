"""R9 验收：数字验证码 / 学生自助注册 / 管理员数据看板。

分三组：

- **验证码**：一码一用、过期、错误文案可区分（1006 而不是 1001）；
- **注册**：学号即用户名、初始口令规则、重复学号回 2010 且**不能绕过验证码去探**；
- **看板**：非管理员一律 403；空库不炸（`met_rate` 为 None 而不是除零）；
  画像与原值归并两套标签并存。看板是只读聚合，用例全部按"塞数据 → 读接口"写。
"""

from __future__ import annotations

import json

import pytest

from app import captcha as captcha_mod
from app.contract import new_ulid
from app.models import Conversation, Document, MatchSession, Message, User, db, utcnow
from shared.enums import ROLE_STUDENT


@pytest.fixture(autouse=True)
def _clean_captcha():
    """验证码存在进程内字典里，用例之间必须隔离。"""
    captcha_mod.reset()
    yield
    captcha_mod.reset()


def _register(client, *, name="张同学", no="240123020027", code="1234",
              captcha_code=None, password=None):
    issued = captcha_mod.issue(plain=code)
    payload = {
        "display_name": name,
        "student_no": no,
        "captcha_id": issued["captcha_id"],
        "captcha_code": captcha_code if captcha_code is not None else code,
    }
    if password is not None:
        payload["password"] = password
    return client.post("/api/v1/auth/register", json=payload)


# ---------------------------------------------------------------- 验证码
def test_captcha_returns_image_and_id_but_not_the_digits(client):
    resp = client.get("/api/v1/auth/captcha")
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert data["captcha_id"]
    assert data["image"].startswith("data:image/svg+xml;base64,")
    # 明文只能出现在服务端存储里：出参不得有 code / digits / answer 这类字段
    assert not {"code", "digits", "answer", "text"} & set(data)


def test_captcha_is_single_use(client):
    issued = captcha_mod.issue(plain="4321")
    captcha_mod.verify(issued["captcha_id"], "4321")
    with pytest.raises(Exception) as exc:
        captcha_mod.verify(issued["captcha_id"], "4321")
    assert getattr(exc.value, "code", None) == 1006


def test_captcha_wrong_code_burns_the_ticket(client):
    """打错一次，这张图就作废——否则 4 位数字可以被慢慢试。"""
    issued = captcha_mod.issue(plain="1234")
    with pytest.raises(Exception) as exc:
        captcha_mod.verify(issued["captcha_id"], "0000")
    assert getattr(exc.value, "code", None) == 1006
    assert captcha_mod.pending_count() == 0


# ---------------------------------------------------------------- 注册
def test_register_creates_student_and_returns_token(client):
    resp = _register(client)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    data = resp.get_json()["data"]
    assert data["token"]
    assert data["user"]["username"] == "240123020027"
    assert data["user"]["display_name"] == "张同学"
    assert data["user"]["role"] == ROLE_STUDENT
    assert data["password_source"] == "student_no_suffix"
    assert "password_hash" not in json.dumps(data)


def test_register_default_password_is_last_six_digits(client):
    """需求只要求填三栏，但登录必须有密码：默认取学号后 6 位，并且要明说。"""
    data = _register(client, no="240123020027").get_json()["data"]
    assert data["initial_password"] == "020027"

    login = client.post("/api/v1/auth/login",
                        json={"username": "240123020027", "password": "020027"})
    assert login.status_code == 200
    assert login.get_json()["code"] == 0


def test_register_with_custom_password(client):
    data = _register(client, password="hello123").get_json()["data"]
    assert data["password_source"] == "custom"
    assert data["initial_password"] is None

    login = client.post("/api/v1/auth/login",
                        json={"username": "240123020027", "password": "hello123"})
    assert login.status_code == 200


def test_register_wrong_captcha_is_1006(client):
    resp = _register(client, captcha_code="0000")
    assert resp.status_code == 400
    assert resp.get_json()["code"] == 1006


def test_register_duplicate_student_no_is_2010(client):
    _register(client)
    resp = _register(client, name="另一个人")
    assert resp.status_code == 409
    assert resp.get_json()["code"] == 2010


def test_duplicate_check_cannot_bypass_captcha(client):
    """拿一个错误验证码去试重复学号，必须卡在验证码这一步。

    否则"该学号已注册"就成了一个不用过验证码的查询接口，可以拿来枚举学号。
    """
    _register(client)
    resp = _register(client, captcha_code="0000")
    assert resp.get_json()["code"] == 1006


def test_register_rejects_short_password_and_bad_student_no(client):
    short = _register(client, password="123")
    assert short.status_code == 400
    assert short.get_json()["code"] == 1001

    # 学号只允许字母数字：中文姓名混进学号栏是最常见的手误
    bad = _register(client, no="学号2401")
    assert bad.status_code == 400
    assert bad.get_json()["code"] == 1001


def test_register_rejects_unknown_field(client):
    """extra='forbid'：多塞一个 role 字段想提权，直接 1001。"""
    issued = captcha_mod.issue(plain="1234")
    resp = client.post("/api/v1/auth/register", json={
        "display_name": "张同学", "student_no": "240123020027",
        "captcha_id": issued["captcha_id"], "captcha_code": "1234",
        "role": "admin",
    })
    assert resp.status_code == 400
    assert resp.get_json()["code"] == 1001


def test_register_still_needs_no_token(client):
    """注册是公开接口：带不带 Authorization 都要能过。"""
    resp = _register(client, no="240123030001")
    assert resp.status_code == 200


# ---------------------------------------------------------------- 看板
def test_analytics_requires_admin(client, student_headers):
    for path in ("/api/v1/admin/analytics/portraits", "/api/v1/admin/analytics/qa"):
        resp = client.get(path, headers=student_headers)
        assert resp.status_code == 403, path
        assert resp.get_json()["code"] == 3002


def test_portraits_on_empty_db_does_not_divide_by_zero(client, admin_headers):
    data = client.get("/api/v1/admin/analytics/portraits", headers=admin_headers).get_json()["data"]
    assert data["summary"]["session_count"] == 0
    assert data["summary"]["met_rate"] is None
    assert data["summary"]["avg_completeness"] is None
    assert data["items"] == []


def _seed_session(app, *, student_type="全日制在校本科生", grade="2", gpa="3.6",
                  met=2, not_met=1, unknown=1):
    with app.app_context():
        student_id = db.session.execute(
            db.select(User.id).where(User.username == "student")).scalar()
        admin_id = db.session.execute(
            db.select(User.id).where(User.username == "admin")).scalar()
        document = Document(
            id=new_ulid(), title="（测试）奖学金办法", file_name="p.pdf",
            stored_name=f"{new_ulid()}.pdf", file_type="pdf", file_size=1024,
            parse_status="indexed", chunk_count=1, uploader_id=admin_id,
        )
        db.session.add(document)
        db.session.add(MatchSession(
            id=new_ulid(), user_id=student_id, document_id=document.id,
            profile_json=json.dumps({
                "student_type": student_type, "grade": grade, "gpa": gpa,
                "rank_percent": "10", "major": "物联网工程",
                "credits_earned": "90", "enrollment_status": "enrolled",
                "poverty_certified": "yes", "disciplinary_record": "none",
            }, ensure_ascii=False),
            rule_snapshot_json="[]",
            sum_met=met, sum_not_met=not_met, sum_unknown=unknown,
        ))
        db.session.commit()


def test_portraits_counts_and_rate(client, admin_headers, app):
    _seed_session(app, met=3, not_met=1, unknown=0)
    data = client.get("/api/v1/admin/analytics/portraits", headers=admin_headers).get_json()["data"]
    summary = data["summary"]
    assert summary["session_count"] == 1
    assert summary["student_count"] == 1
    assert summary["judged"] == 4
    assert summary["met_rate"] == 0.75
    assert summary["unknown_rate"] == 0.0
    # 九个画像字段全填 → 完整度 1.0
    assert summary["avg_completeness"] == 1.0
    item = data["items"][0]
    assert item["student"] == "演示学生"
    assert item["student_type"] == "全日制在校本科生"
    assert item["met"] == 3
    # created_at 必须带 Z：不带的话浏览器会把 UTC 当本地时间显示
    assert item["created_at"].endswith("Z")


def test_portraits_normalizes_legacy_student_type_labels(client, admin_headers, app):
    """历史数据里存过英文枚举值，统计图要归到同一类，但原值必须留痕。"""
    _seed_session(app, student_type="full_time_undergraduate")
    data = client.get("/api/v1/admin/analytics/portraits", headers=admin_headers).get_json()["data"]
    summary = data["summary"]
    assert summary["by_student_type"] == [{"label": "本科生", "count": 1}]
    assert summary["raw_student_types"] == [{"label": "full_time_undergraduate", "count": 1}]


def _seed_messages(app, texts, *, role="user"):
    with app.app_context():
        student_id = db.session.execute(
            db.select(User.id).where(User.username == "student")).scalar()
        conv = Conversation(id=new_ulid(), user_id=student_id, title="测试会话")
        db.session.add(conv)
        for text in texts:
            db.session.add(Message(
                id=new_ulid(), conversation_id=conv.id, role=role,
                content=text, created_at=utcnow(),
            ))
        db.session.commit()


def test_qa_overview_extracts_keywords_and_topics(client, admin_headers, app):
    _seed_messages(app, [
        "国家励志奖学金的申请条件是什么？",
        "奖学金需要提交哪些材料？",
        "奖学金评审结果什么时候公示？",
        "宿舍门禁时间是多少？",
    ])
    data = client.get("/api/v1/admin/analytics/qa", headers=admin_headers).get_json()["data"]
    assert data["summary"]["question_count"] == 4
    assert data["summary"]["asker_count"] == 1
    assert len(data["hours"]) == 24
    assert data["keywords"], "四个句子应当能抽出关键词"
    words = {row["word"] for row in data["keywords"]}
    assert "奖学金" in words
    topics = {row["key"]: row["count"] for row in data["topics"]}
    assert topics.get("scholarship") == 3
    assert topics.get("living") == 1
    # 主题计数之和必须等于问题总数，否则饼图会缺一块
    assert sum(topics.values()) == 4
    first = data["topics"][0]
    assert first["label"] == "奖助学金"
    assert 0 < first["share"] <= 1
    assert first["examples"]


def test_qa_overview_ignores_assistant_messages(client, admin_headers, app):
    """只统计 role=user。把答案也算进去，词云里会全是"政策""条款"这类回显词。"""
    _seed_messages(app, ["奖学金怎么申请"], role="user")
    _seed_messages(app, ["根据《奖学金办法》第三条，申请时应当……"], role="assistant")
    data = client.get("/api/v1/admin/analytics/qa", headers=admin_headers).get_json()["data"]
    assert data["summary"]["question_count"] == 1


def test_qa_overview_on_empty_db(client, admin_headers):
    data = client.get("/api/v1/admin/analytics/qa", headers=admin_headers).get_json()["data"]
    assert data["summary"]["question_count"] == 0
    assert data["summary"]["peak_hour"] is None
    assert data["keywords"] == []
    assert data["topics"] == []
    assert len(data["hours"]) == 24
