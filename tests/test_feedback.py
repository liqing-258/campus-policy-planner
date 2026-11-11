"""答案反馈测试（契约 #15，M6 新增接口）。

一条反馈值不值得存，看三件事：
- **幂等**：同一个人对同一条答案点两次，只能有一条记录，否则"多少人说有用"会虚高；
- **可归因**：踩必须给原因码，否则这条数据没法用来改进任何东西；
- **可校验归属**：只能评价自己会话里的回答，且只能评价助手消息。
"""

from __future__ import annotations

from app.contract import new_ulid
from app.models import Conversation, Message, QaFeedback, User, db


def _seed_answer(app, *, username: str = "student"):
    """建一个会话 + 一问一答。不经过 ask：反馈接口不依赖模型。"""
    with app.app_context():
        user_id = db.session.execute(
            db.select(User.id).where(User.username == username)).scalar_one()
        conversation = Conversation(id=new_ulid(), user_id=user_id, title="测试会话")
        db.session.add(conversation)
        db.session.flush()
        question = Message(id=new_ulid(), conversation_id=conversation.id,
                           role="user", content="申请需要什么材料？", mode="llm")
        answer = Message(id=new_ulid(), conversation_id=conversation.id,
                         role="assistant", content="需要申请表 [1]。", mode="llm")
        db.session.add_all([question, answer])
        db.session.commit()
        return {
            "conversation_id": conversation.id,
            "question_id": question.id,
            "answer_id": answer.id,
            "user_id": user_id,
        }


def post_feedback(client, headers, message_id: str, **payload):
    return client.post(f"/api/v1/messages/{message_id}/feedback",
                       headers=headers, json=payload)


def test_create_feedback(app, client, student_headers):
    seed = _seed_answer(app)
    body = post_feedback(client, student_headers, seed["answer_id"], helpful=True).get_json()
    assert body["code"] == 0, body
    assert body["data"]["helpful"] is True
    assert body["data"]["updated"] is False
    assert body["data"]["feedback_id"]

    with app.app_context():
        assert db.session.execute(
            db.select(db.func.count()).select_from(QaFeedback)
        ).scalar_one() == 1


def test_second_submit_overwrites_instead_of_appending(app, client, student_headers):
    """覆盖而不是新增：否则"这条答案有 5 个人说有用"可能是同一个人点了 5 次。"""
    seed = _seed_answer(app)
    first = post_feedback(client, student_headers, seed["answer_id"], helpful=True).get_json()
    second = post_feedback(client, student_headers, seed["answer_id"],
                           helpful=False, reason_code="wrong_fact",
                           comment="绩点门槛写错了").get_json()

    assert second["code"] == 0, second
    assert second["data"]["updated"] is True
    assert second["data"]["feedback_id"] == first["data"]["feedback_id"]
    assert second["data"]["helpful"] is False

    with app.app_context():
        rows = db.session.execute(db.select(QaFeedback)).scalars().all()
        assert len(rows) == 1
        assert rows[0].helpful == 0
        assert rows[0].reason_code == "wrong_fact"
        assert rows[0].comment == "绩点门槛写错了"


def test_unhelpful_requires_a_reason_code(app, client, student_headers):
    """一条只有"👎"的反馈无法改进任何东西——这是产品判断，写在契约里。"""
    seed = _seed_answer(app)
    resp = post_feedback(client, student_headers, seed["answer_id"], helpful=False)
    body = resp.get_json()
    assert resp.status_code == 400
    assert body["code"] == 1001
    assert "reason_code" in body["message"]


def test_unknown_reason_code_is_rejected(client, app, student_headers):
    seed = _seed_answer(app)
    resp = post_feedback(client, student_headers, seed["answer_id"],
                         helpful=False, reason_code="随便写的")
    assert resp.get_json()["code"] == 1001


def test_cannot_rate_the_question_itself(app, client, student_headers):
    seed = _seed_answer(app)
    resp = post_feedback(client, student_headers, seed["question_id"], helpful=True)
    body = resp.get_json()
    assert resp.status_code == 422
    assert body["code"] == 1003
    assert "助手" in body["message"]


def test_cannot_rate_someone_elses_answer(app, client, student_headers, admin_headers):
    """admin 有全部权限，但"读别人的会话"不在其中：反馈只能给自己的。"""
    seed = _seed_answer(app, username="admin")
    resp = post_feedback(client, student_headers, seed["answer_id"], helpful=True)
    assert resp.status_code == 403
    assert resp.get_json()["code"] == 3002

    # 本人可以
    ok_body = post_feedback(client, admin_headers, seed["answer_id"], helpful=True).get_json()
    assert ok_body["code"] == 0


def test_missing_message_returns_its_own_code(client, student_headers):
    resp = post_feedback(client, student_headers, new_ulid(), helpful=True)
    body = resp.get_json()
    assert resp.status_code == 404
    assert body["code"] == 2009
    assert "消息" in body["message"]


def test_feedback_requires_login(client, app):
    seed = _seed_answer(app)
    resp = client.post(f"/api/v1/messages/{seed['answer_id']}/feedback",
                       json={"helpful": True})
    assert resp.status_code == 401
    assert resp.get_json()["code"] == 3001


def test_message_list_carries_the_feedback(app, client, student_headers):
    """前端回看历史时要能看到"这条我当时点过没用"，否则刷新后状态就丢了。"""
    seed = _seed_answer(app)
    post_feedback(client, student_headers, seed["answer_id"],
                  helpful=False, reason_code="too_vague")

    body = client.get(
        f"/api/v1/conversations/{seed['conversation_id']}/messages",
        headers=student_headers,
    ).get_json()
    answers = [m for m in body["data"]["items"] if m["role"] == "assistant"]
    assert answers[0]["feedback"] == {"helpful": False, "reason_code": "too_vague"}

    questions = [m for m in body["data"]["items"] if m["role"] == "user"]
    assert questions[0]["feedback"] is None
