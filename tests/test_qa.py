"""M4 问答链路测试。

**不调真实模型**：用 monkeypatch 替换 `app.llm._post`，让"模型返回什么"变成
测试数据而不是网络行为。这样 CI 不花钱、不受限流影响、也不会因为模型换了
措辞而随机失败——评测集（tools/run_eval.py）才是验证回答质量的地方，
这里验证的是**代码对模型输出的处理是否正确**。

覆盖的四处高风险：
1. 模型返回非 JSON / 字段形状错 → 必须 4002，不能硬塞进正常答案
2. 模型给出越界或布尔型引用 → 必须剔除，不能让答案"看起来有出处"
3. 推理模型吃光 max_tokens → 必须报预算问题，不能被误判成"模型说没依据"
4. 拒答/降级 → HTTP 必须是 200（业务态），且要带回候选片段
"""

from __future__ import annotations

import json

import pytest

from app.contract import new_ulid
from app.models import Conversation, Document, DocumentChunk, Message, db

# 三条与演示政策高度相关的片段，用来触发覆盖率闸门
CHUNKS = [
    ("第四条 申请国家励志奖学金，应当同时具备下列条件：学年平均学分绩点不低于 3.0，"
     "且专业排名位于前 30%；经学校家庭经济困难学生认定程序认定为家庭经济困难。", 2, "第二章 申请条件"),
    ("第九条 申请国家励志奖学金应当提交下列材料：申请表、上一学年成绩单、"
     "家庭经济困难学生认定结果告知书、个人申请书。", 4, "第四章 申请材料与附则"),
    ("第七条 每学年秋季学期集中受理一次，申请材料提交截止时间为当年 9 月 10 日 17:00。", 3,
     "第三章 评审程序与时间安排"),
]


@pytest.fixture()
def seeded(app):
    """直接写入三个片段并建索引：不依赖 PDF 解析，测试快且内容可控。"""
    with app.app_context():
        document = Document(
            id=new_ulid(), title="本科生奖学金评审办法", file_name="policy.pdf",
            stored_name="policy.pdf", file_type="pdf", file_size=1024,
            parse_status="indexed", chunk_count=len(CHUNKS),
            uploader_id=db.session.execute(db.select(Document.id)).scalar() or new_ulid(),
        )
        uploader = db.session.execute(
            db.select(db.text("id")).select_from(db.text("users"))).first()
        document.uploader_id = uploader[0]
        db.session.add(document)
        for index, (content, page, section) in enumerate(CHUNKS):
            db.session.add(DocumentChunk(
                id=new_ulid(), document_id=document.id, chunk_index=index,
                content=content, char_start=index * 100, char_end=(index + 1) * 100,
                page_number=page, section_title=section,
            ))
        db.session.commit()

        from app.indexing import rebuild_index
        rebuild_index()
        document_id = document.id
    return document_id


def configure_llm(app, *, key: str = "test-key", model: str = "test-model"):
    """把 app 上的配置改成"已接入模型"，否则会走降级分支。

    同时把覆盖率闸门关掉（=0）：这些用例验的是**引用校验与契约校验**，
    不该因为"某条片段覆盖率恰好低于阈值"而失败——那是另一个用例的事
    （test_low_coverage_is_refused_without_calling_model）。
    不关掉的话，测试会随 RAG_MIN_COVERAGE 的调整而集体变红，属于耦合错了对象。
    """
    settings = app.extensions["settings"]
    settings.llm_api_key = key
    settings.llm_model = model
    settings.rag_min_coverage = 0.0


def fake_payload(content: str, *, reasoning: int | None = None,
                 prompt: int = 120, completion: int = 60) -> dict:
    return {
        "model": "fake-model",
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "completion_tokens_details": {"reasoning_tokens": reasoning},
        },
    }


def answer_json(answer: str, cited: list, sufficient: bool = True,
                confidence: str = "high") -> str:
    return json.dumps({"answer": answer, "cited": cited,
                       "sufficient": sufficient, "confidence": confidence},
                      ensure_ascii=False)


def ask(client, headers, question: str = "申请国家励志奖学金需要提交哪些材料？"):
    return client.post("/api/v1/qa/ask", headers=headers, json={"question": question})


# ============================================================ 正常链路
def test_answer_with_valid_citation(app, client, student_headers, seeded, monkeypatch):
    configure_llm(app)
    monkeypatch.setattr("app.llm._post", lambda body: (
        fake_payload(answer_json("需要提交申请表、成绩单与认定告知书 [1][2]。", [1, 2])), 15))

    resp = ask(client, student_headers)
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["code"] == 0, body
    data = body["data"]
    assert "申请表" in data["answer"]
    assert [c["index"] for c in data["citations"]] == [1, 2]
    # 引用必须能落到真实片段上：chunk_id 来自库，不是模型给的
    assert all(c["chunk_id"] for c in data["citations"])
    assert data["citations"][0]["document_title"] == "本科生奖学金评审办法"
    assert data["mode"] == "llm"
    assert data["retrieval_mode"] == "bm25_only"


def test_ask_returns_http_200_even_when_refused(app, client, student_headers, seeded, monkeypatch):
    """契约规范 §3.3：4001/4003/4006 是业务态，HTTP 必须 200。"""
    configure_llm(app)
    monkeypatch.setattr("app.llm._post", lambda body: (
        fake_payload(answer_json("", [], sufficient=False)), 12))
    resp = ask(client, student_headers)
    assert resp.status_code == 200
    assert resp.get_json()["code"] == 4001


# ============================================================ 闸一：覆盖率
def test_low_coverage_is_refused_without_calling_model(app, client, student_headers, seeded,
                                                       monkeypatch):
    """主题不相关时不该调模型——既省钱，也让拒答是确定性的。"""
    configure_llm(app)
    # configure_llm 默认把闸门关掉以便测引用逻辑，这里必须显式打开才验得到本行为
    app.extensions["settings"].rag_min_coverage = 0.6
    called = {"n": 0}

    def _boom(body):
        called["n"] += 1
        raise AssertionError("覆盖率不足时不应调用模型")

    monkeypatch.setattr("app.llm._post", _boom)
    resp = ask(client, student_headers, "图书馆的开馆时间是几点？")
    body = resp.get_json()
    assert body["code"] == 4001
    assert called["n"] == 0
    assert body["data"]["refused"] is True
    assert body["data"]["suggestions"]


def test_empty_knowledge_base_is_refused(app, client, student_headers):
    configure_llm(app)
    resp = ask(client, student_headers)
    body = resp.get_json()
    assert body["code"] == 4001
    assert "知识库为空" in body["message"]


# ============================================================ 闸二：契约校验
def test_non_json_output_raises_4002(app, client, student_headers, seeded, monkeypatch):
    configure_llm(app)
    monkeypatch.setattr("app.llm._post", lambda body: (fake_payload("我觉得应该交申请表。"), 10))
    resp = ask(client, student_headers)
    assert resp.get_json()["code"] == 4002
    assert resp.status_code == 502


def test_bad_field_shape_raises_4002(app, client, student_headers, seeded, monkeypatch):
    """合法 JSON、但 cited 不是整数数组 → 必须 4002，不能凑合解析。"""
    configure_llm(app)
    bad = json.dumps({"answer": "要交申请表", "cited": "第1条", "sufficient": True})
    monkeypatch.setattr("app.llm._post", lambda body: (fake_payload(bad), 10))
    resp = ask(client, student_headers)
    assert resp.get_json()["code"] == 4002


def test_missing_required_shape_falls_back_to_defaults(app, client, student_headers, seeded,
                                                      monkeypatch):
    """只给 answer 不给 cited：schema 有默认值，按默认走，随后被引用闸拦下。"""
    configure_llm(app)
    monkeypatch.setattr("app.llm._post", lambda body: (
        fake_payload(json.dumps({"answer": "要交申请表和成绩单。"})), 10))
    resp = ask(client, student_headers)
    # 有答案但无任何有效引用 → 4003，而不是当成正常回答返回
    assert resp.get_json()["code"] == 4003


def test_reasoning_budget_exhaustion_is_reported_not_treated_as_refusal(
        app, client, student_headers, seeded, monkeypatch):
    """推理模型吃光 max_tokens 时正文为空——必须报 4002，不能误判成"模型说没依据"。

    这是 M4 实测踩到的坑：max_tokens=100 时 reasoning 占满、content 返回 ''。
    若当成拒答，用户会看到"未找到足够依据"，而真实原因是配置问题。
    """
    configure_llm(app)
    monkeypatch.setattr("app.llm._post", lambda body: (
        fake_payload("", reasoning=100, completion=100), 10))
    resp = ask(client, student_headers)
    body = resp.get_json()
    assert body["code"] == 4002
    assert "reasoning_tokens" in body["message"]


# ============================================================ 闸三：引用
def test_all_citations_out_of_range_raises_4003(app, client, student_headers, seeded, monkeypatch):
    configure_llm(app)
    monkeypatch.setattr("app.llm._post", lambda body: (
        fake_payload(answer_json("需要提交申请表 [99]。", [99])), 10))
    resp = ask(client, student_headers)
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["code"] == 4003
    assert body["data"]["rejected_indexes"] == [99]


def test_partially_out_of_range_keeps_valid_and_warns(app, client, student_headers, seeded,
                                                      monkeypatch):
    configure_llm(app)
    monkeypatch.setattr("app.llm._post", lambda body: (
        fake_payload(answer_json("需要申请表与成绩单 [1][42]。", [1, 42])), 10))
    body = ask(client, student_headers).get_json()
    assert body["code"] == 0
    data = body["data"]
    assert [c["index"] for c in data["citations"]] == [1]
    assert data["citation_warnings"]["rejected_indexes"] == [42]


def test_boolean_citation_is_dropped_not_mapped_to_chunk_one(app, client, student_headers,
                                                             seeded, monkeypatch):
    """`cited: [true]` 会被 pydantic 宽松模式转成 `[1]`，从而误指片段 1。

    这类"看起来合法的错误引用"比明显越界更危险，必须在 schema 层就滤掉布尔值。
    """
    configure_llm(app)
    monkeypatch.setattr("app.llm._post", lambda body: (
        fake_payload(json.dumps({"answer": "要交申请表。", "cited": [True],
                                 "sufficient": True}, ensure_ascii=False)), 10))
    body = ask(client, student_headers).get_json()
    # 布尔被滤掉后没有有效引用 → 4003
    assert body["code"] == 4003


def test_duplicate_citations_are_collapsed(app, client, student_headers, seeded, monkeypatch):
    configure_llm(app)
    monkeypatch.setattr("app.llm._post", lambda body: (
        fake_payload(answer_json("需要申请表 [1][1][2]。", [1, 1, 2])), 10))
    data = ask(client, student_headers).get_json()["data"]
    assert [c["index"] for c in data["citations"]] == [1, 2]


# ============================================================ 降级
def test_degraded_mode_returns_raw_candidates(app, client, student_headers, seeded):
    """未配置模型不是错误：返回 4006 + 原文片段，链路照跑。"""
    # conftest 里 llm_api_key 为空，即天然降级
    resp = ask(client, student_headers)
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["code"] == 4006
    data = body["data"]
    assert data["mode"] == "degraded"
    assert data["answer"] is None
    assert data["candidates"]


# ============================================================ 会话与消息
def test_messages_are_persisted(app, client, student_headers, seeded, monkeypatch):
    configure_llm(app)
    monkeypatch.setattr("app.llm._post", lambda body: (
        fake_payload(answer_json("需要申请表与成绩单 [1][2]。", [1, 2])), 10))

    data = ask(client, student_headers).get_json()["data"]
    conversation_id = data["conversation_id"]

    with app.app_context():
        messages = db.session.execute(
            db.select(Message).where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at)
        ).scalars().all()
        assert [m.role for m in messages] == ["user", "assistant"]
        assert messages[1].citations_json is not None
        assert messages[1].model_name == "fake-model"
        assert json.loads(messages[1].citations_json)[0]["index"] == 1


def test_follow_up_in_same_conversation(app, client, student_headers, seeded, monkeypatch):
    configure_llm(app)
    monkeypatch.setattr("app.llm._post", lambda body: (
        fake_payload(answer_json("需要申请表 [1]。", [1])), 10))

    first = ask(client, student_headers).get_json()["data"]
    resp2 = client.post("/api/v1/qa/ask", headers=student_headers,
                        json={"question": "那成绩单要原件吗？",
                              "conversation_id": first["conversation_id"]})
    second = resp2.get_json()["data"]
    assert resp2.get_json()["code"] == 0, resp2.get_json()
    assert second["conversation_id"] == first["conversation_id"]

    with app.app_context():
        count = db.session.execute(
            db.select(db.func.count()).select_from(Message)
            .where(Message.conversation_id == first["conversation_id"])).scalar_one()
        assert count == 4


def test_other_users_conversation_is_forbidden(app, client, student_headers, admin_headers,
                                               seeded, monkeypatch):
    configure_llm(app)
    monkeypatch.setattr("app.llm._post", lambda body: (
        fake_payload(answer_json("需要申请表 [1]。", [1])), 10))

    conversation_id = ask(client, student_headers).get_json()["data"]["conversation_id"]
    resp = client.post("/api/v1/qa/ask", headers=admin_headers,
                       json={"question": "再问一次", "conversation_id": conversation_id})
    assert resp.get_json()["code"] == 3002


def test_unknown_conversation_returns_2002(app, client, student_headers, seeded, monkeypatch):
    configure_llm(app)
    monkeypatch.setattr("app.llm._post", lambda body: (
        fake_payload(answer_json("需要申请表 [1]。", [1])), 10))
    resp = client.post("/api/v1/qa/ask", headers=student_headers,
                       json={"question": "再问一次", "conversation_id": "01JZZZZZZZZZZZZZZZZZZZZZZZ"})
    assert resp.get_json()["code"] == 2002


# ============================================================ 检索接口
def test_search_endpoint_returns_candidates(app, client, student_headers, seeded):
    resp = client.post("/api/v1/retrieval/search", headers=student_headers,
                       json={"query": "国家励志奖学金 申请条件", "top_k": 3})
    body = resp.get_json()
    assert body["code"] == 0
    data = body["data"]
    assert data["candidates"]
    assert data["mode"] == "bm25_only"
    assert data["candidates"][0]["coverage"] > 0
    assert "content" in data["candidates"][0]


def test_search_endpoint_requires_auth(client, seeded):
    resp = client.post("/api/v1/retrieval/search", json={"query": "奖学金"})
    assert resp.get_json()["code"] == 3001


def test_search_rejects_unknown_field(app, client, student_headers, seeded):
    resp = client.post("/api/v1/retrieval/search", headers=student_headers,
                       json={"query": "奖学金", "unexpected": 1})
    assert resp.get_json()["code"] == 1001


def test_ask_rejects_empty_question(client, student_headers, seeded):
    resp = client.post("/api/v1/qa/ask", headers=student_headers, json={"question": ""})
    assert resp.get_json()["code"] == 1001


# ============================================================ 会话列表
def test_conversations_are_scoped_to_current_user(app, client, student_headers,
                                                  admin_headers, seeded, monkeypatch):
    configure_llm(app)
    monkeypatch.setattr("app.llm._post", lambda body: (
        fake_payload(answer_json("需要申请表 [1]。", [1])), 10))
    ask(client, student_headers)

    student_list = client.get("/api/v1/conversations", headers=student_headers).get_json()["data"]
    admin_list = client.get("/api/v1/conversations", headers=admin_headers).get_json()["data"]
    assert student_list["total"] == 1
    assert admin_list["total"] == 0
