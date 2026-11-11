"""M6 节点轨迹测试（FR-13）。

验三件事，按重要性排：

1. **轨迹是真实的**：执行了几个节点就写几个 `succeeded`，没执行的写 `skipped`
   并带原因。把没跑的节点补成"已完成"是这个模块最容易犯的错，
   也是最难被发现的错——界面上看着更漂亮，但每个数字都是假的。
2. **拒答/降级/失败也落轨迹**：这三条路径都会抛业务态异常，
   如果轨迹跟着业务数据一起被回滚，"为什么这次没答出来"就永远查不到。
3. **轨迹有归属**：别人拿到 run_id 也查不到。
"""

from __future__ import annotations

import pytest

from app.contract import new_ulid
from app.models import WorkflowRun, db
from app.workflows import WorkflowRecorder


def answer_payload(answer: str, cited: list, *, sufficient: bool = True) -> str:
    import json

    return json.dumps(
        {"answer": answer, "cited": cited, "sufficient": sufficient,
         "confidence": "high"},
        ensure_ascii=False,
    )


def ask(client, headers, question: str = "申请国家励志奖学金需要提交哪些材料？", **extra):
    payload = {"question": question, **extra}
    return client.post("/api/v1/qa/ask", headers=headers, json=payload)


def nodes_of(workflow: dict) -> dict[str, dict]:
    return {node["node"]: node for node in workflow["nodes"]}


# ============================================================ 成功路径
def test_successful_ask_records_four_executed_nodes(app, client, student_headers,
                                                    seeded_policy, llm_on, mock_llm):
    llm_on()
    mock_llm(answer_payload("需要提交申请表与成绩单 [1][2]。", [1, 2]))

    body = ask(client, student_headers).get_json()
    assert body["code"] == 0, body

    workflow = body["data"]["workflow"]
    assert workflow["status"] == "succeeded"
    assert workflow["outcome"] == "answered"
    # 六节点一律出现在轨迹里：缺节点会被误读成"那一步失败了"
    assert workflow["node_count"] == 6
    assert [n["node"] for n in workflow["nodes"]] == [
        "intent", "retrieval", "analysis", "matching", "planning", "validation",
    ]
    assert workflow["executed_count"] == 4

    nodes = nodes_of(workflow)
    for name in ("intent", "retrieval", "analysis", "validation"):
        assert nodes[name]["status"] == "succeeded", nodes[name]
    for name in ("matching", "planning"):
        assert nodes[name]["status"] == "skipped"
        # 跳过必须给原因，而不是一句空的 "skipped"
        assert "问答链路" in nodes[name]["summary"]
    assert nodes["intent"]["label"] == "意图识别"


def test_only_analysis_node_calls_the_model(app, client, student_headers,
                                            seeded_policy, llm_on, mock_llm):
    """六个节点里只有一个调模型——这是设计 §7.6 的硬边界，也决定了延迟构成。"""
    llm_on()
    mock_llm(answer_payload("需要提交申请表 [1]。", [1]))

    workflow = ask(client, student_headers).get_json()["data"]["workflow"]
    nodes = nodes_of(workflow)
    assert nodes["analysis"]["model_calls"] == 1
    assert nodes["intent"]["model_calls"] == 0
    assert nodes["retrieval"]["model_calls"] == 0
    assert workflow["model_calls"] == 1
    # 意图节点是规则式的，耗时不该有第二名——真调了模型这里会明显变大
    assert nodes["intent"]["ms"] < 50


def test_intent_node_output_drives_next_actions(app, client, student_headers,
                                                seeded_policy, llm_on, mock_llm):
    llm_on()
    mock_llm(answer_payload("绩点不低于 3.0 [1]。", [1]))
    data = ask(client, student_headers, "申请励志奖学金要满足什么条件？").get_json()["data"]

    assert data["matter"]["type"] == "scholarship"
    assert data["matter"]["label"] == "奖助学金"
    # 问的是条件，才建议去做资格匹配；只问截止日期的就不该推荐
    assert any(action["action"] == "match" for action in data["next_actions"])
    assert nodes_of(data["workflow"])["intent"]["status"] == "succeeded"


def test_flow_question_gets_no_match_suggestion(app, client, student_headers,
                                                seeded_policy, llm_on, mock_llm):
    llm_on()
    mock_llm(answer_payload("截止时间为当年 9 月 10 日 [1]。", [1]))
    data = ask(client, student_headers, "奖学金材料什么时候截止？").get_json()["data"]
    assert data["next_actions"] == []


# ============================================================ 非成功路径
def test_refusal_still_records_run(app, client, student_headers, seeded_policy,
                                   llm_on, mock_llm):
    """拒答是业务态 4001，但运行记录必须留下来——否则查不到为什么拒答。"""
    llm_on()
    mock_llm(answer_payload("", [], sufficient=False))

    body = ask(client, student_headers, "量子纠缠实验怎么做？").get_json()
    assert body["code"] == 4001, body
    workflow = body["data"]["workflow"]
    assert workflow["status"] == "succeeded"
    assert workflow["outcome"] == "refused"
    nodes = nodes_of(workflow)
    # 检索阶段就否决了，模型没被调用
    assert nodes["analysis"]["status"] == "skipped"
    assert "未调用模型" in nodes["analysis"]["summary"]

    with app.app_context():
        assert db.session.execute(
            db.select(db.func.count()).select_from(WorkflowRun)
        ).scalar_one() == 1


def test_model_says_insufficient_records_validation_node(app, client, student_headers,
                                                         seeded_policy, llm_on, mock_llm):
    """检索通过了、模型看完说不够：分析节点成功，校验节点判定拒答。"""
    llm_on()
    mock_llm(answer_payload("", [], sufficient=False))

    body = ask(client, student_headers).get_json()
    assert body["code"] == 4001, body
    nodes = nodes_of(body["data"]["workflow"])
    assert nodes["analysis"]["status"] == "succeeded"
    assert nodes["validation"]["status"] == "succeeded"
    assert "证据不足" in nodes["validation"]["summary"]


def test_degraded_run_marks_analysis_skipped(app, client, student_headers,
                                             seeded_policy):
    """没配模型时不是崩溃，是既定降级：分析节点显式跳过并说明原因。"""
    body = ask(client, student_headers).get_json()
    assert body["code"] == 4006, body
    workflow = body["data"]["workflow"]
    assert workflow["outcome"] == "degraded"
    assert workflow["status"] == "degraded"
    nodes = nodes_of(workflow)
    assert nodes["analysis"]["status"] == "skipped"
    assert "未配置模型" in nodes["analysis"]["summary"]
    assert nodes["retrieval"]["status"] == "succeeded"


def test_invalid_model_output_records_failed_run(app, client, student_headers,
                                                 seeded_policy, llm_on, mock_llm):
    llm_on()
    mock_llm("这不是 JSON，模型跑飞了")

    body = ask(client, student_headers).get_json()
    assert body["code"] == 4002, body
    workflow = body["data"]["workflow"]
    assert workflow["status"] == "failed"
    assert workflow["outcome"] == "failed"
    assert "error" in workflow
    assert nodes_of(workflow)["analysis"]["status"] == "failed"


# ============================================================ 回看与归属
def test_run_can_be_fetched_by_owner(app, client, student_headers,
                                     seeded_policy, llm_on, mock_llm):
    llm_on()
    mock_llm(answer_payload("需要提交申请表 [1]。", [1]))
    run_id = ask(client, student_headers).get_json()["data"]["workflow"]["run_id"]

    resp = client.get(f"/api/v1/workflows/{run_id}", headers=student_headers)
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["code"] == 0, body
    assert body["data"]["run_id"] == run_id
    assert body["data"]["executed_count"] == 4
    assert len(body["data"]["nodes"]) == 6


def test_message_carries_the_run_id(app, client, student_headers,
                                    seeded_policy, llm_on, mock_llm):
    """消息与轨迹要绑在一起：否则"这条答案当时卡在哪"只能靠时间戳猜。"""
    llm_on()
    mock_llm(answer_payload("需要提交申请表 [1]。", [1]))
    data = ask(client, student_headers).get_json()["data"]

    body = client.get(
        f"/api/v1/conversations/{data['conversation_id']}/messages",
        headers=student_headers,
    ).get_json()
    assistant = [m for m in body["data"]["items"] if m["role"] == "assistant"]
    assert assistant[0]["workflow_run_id"] == data["workflow"]["run_id"]


def test_other_users_run_is_forbidden(app, client, student_headers, admin_headers,
                                      seeded_policy, llm_on, mock_llm):
    llm_on()
    mock_llm(answer_payload("需要提交申请表 [1]。", [1]))
    run_id = ask(client, student_headers).get_json()["data"]["workflow"]["run_id"]

    resp = client.get(f"/api/v1/workflows/{run_id}", headers=admin_headers)
    assert resp.status_code == 403
    assert resp.get_json()["code"] == 3002


def test_missing_run_returns_404_with_its_own_code(client, student_headers):
    """不能复用 2002（文档不存在）：报错要能指向正确的对象类型。"""
    resp = client.get(f"/api/v1/workflows/{new_ulid()}", headers=student_headers)
    body = resp.get_json()
    assert resp.status_code == 404
    assert body["code"] == 2009
    assert "工作流记录" in body["message"]


# ============================================================ 记录器本身
def test_recorder_marks_failed_node_and_reraises(app, client):
    """节点内抛异常：轨迹记 failed，异常原样抛出。吞掉异常的可观测性是负的。"""
    with app.app_context():
        recorder = WorkflowRecorder(new_ulid(), "qa")
        with pytest.raises(RuntimeError, match="boom"):
            with recorder.node("retrieval"):
                raise RuntimeError("boom")
        trace = recorder.nodes["retrieval"]
        assert trace.status == "failed"
        assert "RuntimeError" in trace.error


def test_finish_is_idempotent(app, client):
    """同一次问答只能有一条运行记录。

    降级路径会先 finish() 拿 run_id 写进消息、提交后才抛异常，
    外层 except 收尾时还会 persist 一次——没有幂等保护就会留下两条。
    """
    with app.app_context():
        recorder = WorkflowRecorder(new_ulid(), "qa")
        first = recorder.finish("answered")
        second = recorder.finish("answered")
        assert first is second
        assert len(db.session.new) == 1
