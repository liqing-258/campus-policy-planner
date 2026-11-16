"""常用政策收藏（R17）测试。

这一层最怕的不是 500，而是四件「看起来正常」的错：

- 收藏数据没按账号隔离（学生 A 的收藏被学生 B 看到）；
- 收藏只存前端临时变量，刷新/换端就丢（必须持久化到 favorites 表）；
- 政策更新后收藏列表还显示旧的有效状态（需求要求「同步显示最新有效状态」）；
- 收藏问答丢了原始回答与来源（需求要求「保留原始回答和来源信息」）。

断言打在具体字段、具体状态、具体隔离上，不满足「接口 200 且非空」。
"""

from __future__ import annotations

import json

from app.contract import new_ulid
from app.models import (
    ActionPlan,
    Conversation,
    Document,
    Favorite,
    Message,
    User,
    db,
)


def _login(client, username="student", password="student123") -> dict:
    resp = client.post("/api/v1/auth/login",
                       json={"username": username, "password": password})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return {"Authorization": f"Bearer {resp.get_json()['data']['token']}"}


def _student(app, username="student") -> User:
    return db.session.execute(
        db.select(User).where(User.username == username)).scalars().first()


def _doc(app, title="本科生奖学金评审办法", effective="active") -> str:
    with app.app_context():
        doc = Document(
            id=new_ulid(), title=title, file_name=f"{title}.pdf",
            stored_name=f"{new_ulid()}.pdf", file_type="pdf", file_size=1024,
            file_hash=new_ulid(), effective_status=effective,
            parse_status="indexed", uploader_id=_student(app).id,
        )
        db.session.add(doc)
        db.session.commit()
        return doc.id


def _qa_pair(app, question="国家奖学金什么时候申请？",
             answer="国家奖学金每年秋季学期评审。",
             citations=None) -> tuple[str, str]:
    """造一对 user + assistant 消息，返回 (user_id, assistant_id)。"""
    with app.app_context():
        conv = Conversation(id=new_ulid(), user_id=_student(app).id,
                            title="测试会话")
        db.session.add(conv)
        q = Message(id=new_ulid(), conversation_id=conv.id, role="user",
                    content=question)
        a = Message(id=new_ulid(), conversation_id=conv.id, role="assistant",
                    content=answer, citations_json=json.dumps(citations)
                    if citations else None)
        db.session.add_all([q, a])
        db.session.commit()
        return q.id, a.id


def _plan(app, document_id: str, title="申请励志奖学金计划") -> str:
    with app.app_context():
        plan = ActionPlan(
            id=new_ulid(), user_id=_student(app).id, document_id=document_id,
            title=title, status="confirmed",
        )
        db.session.add(plan)
        db.session.commit()
        return plan.id


def _favorite(app, target_type, target_id, *, user="student",
              title="快照标题", document_id=None) -> str:
    with app.app_context():
        row = Favorite(
            id=new_ulid(), user_id=_student(app, user).id,
            target_type=target_type, target_id=target_id,
            title=title, document_id=document_id,
        )
        db.session.add(row)
        db.session.commit()
        return row.id


# ---------------------------------------------------------------- 创建收藏
def test_favorite_document(client, student_headers, app):
    doc = _doc(app)
    resp = client.post("/api/v1/favorites", headers=student_headers,
                       json={"target_type": "document", "target_id": doc})
    assert resp.status_code == 201
    data = resp.get_json()["data"]
    assert data["updated"] is False
    assert data["target_type"] == "document"
    assert data["title"] == "本科生奖学金评审办法"
    assert data["document_id"] == doc


def test_favorite_guide_resolves_policy(client, student_headers, app):
    """收藏指南事项：后端实时解析它指向的政策文件与有效状态。"""
    doc = _doc(app, title="本科生奖学金评审办法")
    resp = client.post("/api/v1/favorites", headers=student_headers,
                       json={"target_type": "guide", "target_id": "inspiring-scholarship"})
    assert resp.status_code == 201
    data = resp.get_json()["data"]
    assert data["title"] == "国家励志奖学金申请"
    assert data["document_id"] == doc


def test_favorite_qa_keeps_answer_and_source(client, student_headers, app):
    doc = _doc(app)
    q, a = _qa_pair(app, question="国家奖学金什么时候申请？",
                    answer="国家奖学金每年秋季学期评审。",
                    citations=[{"index": 1, "document_id": doc,
                                "document_title": "本科生奖学金评审办法",
                                "section_title": "第三章", "snippet": "评审程序"}])
    resp = client.post("/api/v1/favorites", headers=student_headers,
                       json={"target_type": "qa", "target_id": a})
    assert resp.status_code == 201
    data = resp.get_json()["data"]
    assert data["title"] == "国家奖学金什么时候申请？"
    assert data["document_id"] == doc

    # 列表里要能看到原始回答 + 来源
    listing = client.get("/api/v1/favorites", headers=student_headers).get_json()["data"]
    item = listing["items"][0]
    assert item["question"] == "国家奖学金什么时候申请？"
    assert item["answer"] == "国家奖学金每年秋季学期评审。"
    assert item["citations"][0]["document_title"] == "本科生奖学金评审办法"


def test_favorite_plan(client, student_headers, app):
    doc = _doc(app)
    plan = _plan(app, doc)
    resp = client.post("/api/v1/favorites", headers=student_headers,
                       json={"target_type": "plan", "target_id": plan})
    assert resp.status_code == 201
    data = resp.get_json()["data"]
    assert data["title"] == "申请励志奖学金计划"
    assert data["document_id"] == doc


def test_favorite_idempotent(client, student_headers, app):
    doc = _doc(app)
    first = client.post("/api/v1/favorites", headers=student_headers,
                        json={"target_type": "document", "target_id": doc})
    second = client.post("/api/v1/favorites", headers=student_headers,
                         json={"target_type": "document", "target_id": doc})
    assert first.get_json()["data"]["updated"] is False
    assert second.get_json()["data"]["updated"] is True
    with app.app_context():
        count = db.session.execute(
            db.select(db.func.count()).select_from(Favorite)
        ).scalar()
    assert count == 1


def test_favorite_bad_target_type(client, student_headers):
    resp = client.post("/api/v1/favorites", headers=student_headers,
                       json={"target_type": "chatbot", "target_id": "x"})
    assert resp.get_json()["code"] == 1001


def test_favorite_missing_document(client, student_headers):
    resp = client.post("/api/v1/favorites", headers=student_headers,
                       json={"target_type": "document", "target_id": "不存在"})
    assert resp.get_json()["code"] == 2002


def test_favorite_missing_guide(client, student_headers):
    resp = client.post("/api/v1/favorites", headers=student_headers,
                       json={"target_type": "guide", "target_id": "不存在"})
    assert resp.get_json()["code"] == 2011


def test_favorite_requires_login(client):
    resp = client.post("/api/v1/favorites",
                       json={"target_type": "document", "target_id": "x"})
    assert resp.get_json()["code"] == 3001


def test_favorite_others_qa_forbidden(client, student_headers, app):
    """只能收藏自己会话里的回答——收藏不能泄露别人的问答。"""
    # 造一个属于「别人」的 assistant 消息（分步 commit，避免 FK 依赖排序不可靠）
    with app.app_context():
        other = User(id=new_ulid(), username="other", display_name="别人",
                     password_hash="x", role="student")
        db.session.add(other)
        db.session.commit()
        other_id = other.id
    with app.app_context():
        conv = Conversation(id=new_ulid(), user_id=other_id, title="别人的会话")
        db.session.add(conv)
        db.session.commit()
        a = Message(id=new_ulid(), conversation_id=conv.id, role="assistant",
                    content="别人的回答")
        db.session.add(a)
        db.session.commit()
        other_a = a.id
    resp = client.post("/api/v1/favorites", headers=student_headers,
                       json={"target_type": "qa", "target_id": other_a})
    assert resp.get_json()["code"] == 3002


# ---------------------------------------------------------------- 取消收藏
def test_unfavorite(client, student_headers, app):
    doc = _doc(app)
    client.post("/api/v1/favorites", headers=student_headers,
                json={"target_type": "document", "target_id": doc})
    resp = client.delete("/api/v1/favorites/document/" + doc,
                         headers=student_headers)
    assert resp.get_json()["data"]["removed"] is True
    with app.app_context():
        count = db.session.execute(
            db.select(db.func.count()).select_from(Favorite)
        ).scalar()
    assert count == 0


def test_unfavorite_idempotent(client, student_headers):
    resp = client.delete("/api/v1/favorites/document/不存在",
                         headers=student_headers)
    assert resp.status_code == 200
    assert resp.get_json()["data"]["removed"] is False


# ---------------------------------------------------------------- 列表
def test_list_and_type_filter(client, student_headers, app):
    doc = _doc(app)
    q, a = _qa_pair(app)
    client.post("/api/v1/favorites", headers=student_headers,
                json={"target_type": "document", "target_id": doc})
    client.post("/api/v1/favorites", headers=student_headers,
                json={"target_type": "qa", "target_id": a})

    all_items = client.get("/api/v1/favorites", headers=student_headers).get_json()["data"]
    assert all_items["total"] == 2

    only_doc = client.get("/api/v1/favorites?type=document",
                          headers=student_headers).get_json()["data"]
    assert only_doc["total"] == 1
    assert only_doc["items"][0]["target_type"] == "document"


def test_list_keyword_search(client, student_headers, app):
    doc = _doc(app, title="学生宿舍管理规定")
    client.post("/api/v1/favorites", headers=student_headers,
                json={"target_type": "document", "target_id": doc})
    resp = client.get("/api/v1/favorites?q=宿舍",
                      headers=student_headers).get_json()["data"]
    assert resp["total"] == 1
    resp2 = client.get("/api/v1/favorites?q=奖学金",
                       headers=student_headers).get_json()["data"]
    assert resp2["total"] == 0


def test_list_search_matches_qa_body(client, student_headers, app):
    """关键词也要能搜到问答正文（不只是标题快照）。"""
    q, a = _qa_pair(app, question="如何申请宿舍调换？",
                    answer="调宿在每学期第 2 至 14 周受理。")
    client.post("/api/v1/favorites", headers=student_headers,
                json={"target_type": "qa", "target_id": a})
    resp = client.get("/api/v1/favorites?q=调宿",
                      headers=student_headers).get_json()["data"]
    assert resp["total"] == 1


def test_favorite_ids(client, student_headers, app):
    doc = _doc(app)
    client.post("/api/v1/favorites", headers=student_headers,
                json={"target_type": "document", "target_id": doc})
    data = client.get("/api/v1/favorites/ids", headers=student_headers).get_json()["data"]
    assert data["count"] == 1
    assert data["ids"] == [f"document:{doc}"]


# ---------------------------------------------------------------- 账号隔离
def test_isolation_between_students(client, student_headers, app):
    doc = _doc(app)
    client.post("/api/v1/favorites", headers=student_headers,
                json={"target_type": "document", "target_id": doc})

    # 第二个学生
    with app.app_context():
        from app.auth import hash_password
        db.session.add(User(id=new_ulid(), username="stu2", display_name="学生二",
                            password_hash=hash_password("stu2pass"), role="student"))
        db.session.commit()
    other_headers = _login(client, username="stu2", password="stu2pass")

    other = client.get("/api/v1/favorites", headers=other_headers).get_json()["data"]
    assert other["total"] == 0
    ids = client.get("/api/v1/favorites/ids", headers=other_headers).get_json()["data"]
    assert ids["count"] == 0


# ---------------------------------------------------------------- 状态同步
def test_document_status_syncs_after_update(client, student_headers, app):
    """收藏政策后，原政策失效要同步显示最新有效状态（需求五.3）。"""
    doc = _doc(app, effective="active")
    client.post("/api/v1/favorites", headers=student_headers,
                json={"target_type": "document", "target_id": doc})

    # 管理员把这份政策标成「已失效」
    with app.app_context():
        d = db.session.get(Document, doc)
        d.effective_status = "expired"
        db.session.commit()

    item = client.get("/api/v1/favorites",
                      headers=student_headers).get_json()["data"]["items"][0]
    assert item["effective_status"] == "expired"


def test_guide_status_syncs_after_update(client, student_headers, app):
    """收藏指南事项后，其指向的政策换版/失效，收藏里实时反映。"""
    _doc(app, title="本科生奖学金评审办法", effective="active")
    client.post("/api/v1/favorites", headers=student_headers,
                json={"target_type": "guide", "target_id": "inspiring-scholarship"})

    with app.app_context():
        d = db.session.execute(
            db.select(Document).where(Document.title == "本科生奖学金评审办法")
        ).scalar_one()
        d.effective_status = "superseded"
        db.session.commit()

    item = client.get("/api/v1/favorites",
                      headers=student_headers).get_json()["data"]["items"][0]
    assert item["effective_status"] == "superseded"


def test_favorite_title_snapshot_survives_document_delete(client, student_headers, app):
    """目标被下架后，收藏列表仍能用快照标题显示，不会整条消失。"""
    doc = _doc(app, title="一份即将下架的政策")
    client.post("/api/v1/favorites", headers=student_headers,
                json={"target_type": "document", "target_id": doc})
    with app.app_context():
        from app.models import utcnow
        d = db.session.get(Document, doc)
        d.deleted_at = utcnow()  # 软删除
        db.session.commit()
    item = client.get("/api/v1/favorites",
                      headers=student_headers).get_json()["data"]["items"][0]
    assert item["title"] == "一份即将下架的政策"
    assert item["document"] is None
    assert item["effective_status"] is None


# ============================================================ R19：并发写入的防线
def test_favorites_unique_constraint_exists(app):
    """数据层必须有 (user_id, target_type, target_id) 唯一约束。

    为什么单独断言"约束存在"：R19 并发实测（5 线程同时 POST /favorites）里，
    应用层的"先查再插"会同时查到"不存在"，最后只有唯一约束能挡住重复数据。
    约束一旦被删（比如换库、手写迁移），应用层代码看起来没变，重复收藏却会悄悄出现。
    """
    from sqlalchemy import inspect
    with app.app_context():
        inspector = inspect(db.engine)
        # 三元组写成的是 UniqueConstraint（表级约束），**不会**出现在 get_indexes() 里。
        # 我第一次用 get_indexes() 断言，误判成"约束不存在"——查约束要用 get_unique_constraints()。
        constraints = inspector.get_unique_constraints("favorites")
    unique_sets = [set(c["column_names"]) for c in constraints]
    assert any({"user_id", "target_type", "target_id"} <= cols for cols in unique_sets), \
        f"favorites 缺少三元组唯一约束，实际约束：{constraints}"
