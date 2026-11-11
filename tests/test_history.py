"""多轮对话上下文测试（M6 补的 M5 遗留项）。

历史进 Prompt 这件事**很容易做错**，而且错了不容易发现：

- 不带历史：追问"那成绩单呢"时，模型看不到上一句在问什么，只能靠检索碰运气；
- 带了但不设限：第 20 轮对话的 prompt_tokens 会到几千，其中九成是旧答案，
  真正需要的是"上一句问的是什么"；
- 带了但不说清界限：模型会把上一轮的数字当成本轮的政策抄下来，
  于是答案看起来有依据，实际上依据是它自己上一轮说的话。

所以这里的断言分三类：**进去了**（历史真的拼进 Prompt）、**受控**（有轮数上限与
截断）、**划清界限**（Prompt 里明确写着历史不能作为引用依据）。
"""

from __future__ import annotations

ANSWER = '{"answer":"需要提交申请表与成绩单 [1][2]。","cited":[1,2],' \
         '"sufficient":true,"confidence":"high"}'


def ask(client, headers, question: str, **extra):
    return client.post("/api/v1/qa/ask", headers=headers,
                       json={"question": question, **extra})


def test_first_turn_has_no_history_block(client, student_headers, seeded_policy,
                                         llm_on, mock_llm):
    llm_on()
    mock_llm(ANSWER)
    body = ask(client, student_headers, "申请国家励志奖学金需要提交哪些材料？").get_json()
    assert body["code"] == 0, body
    assert "【对话历史】" not in mock_llm.calls["prompt"]
    assert body["data"]["history_pairs"] == 0


def test_second_turn_carries_the_previous_question(client, student_headers, seeded_policy,
                                                   llm_on, mock_llm):
    llm_on()
    mock_llm(ANSWER)
    first = ask(client, student_headers, "申请国家励志奖学金需要提交哪些材料？").get_json()
    conversation_id = first["data"]["conversation_id"]

    second = ask(client, student_headers, "那成绩单要原件吗？",
                 conversation_id=conversation_id).get_json()
    assert second["code"] == 0, second
    prompt = mock_llm.calls["prompt"]
    assert "【对话历史】" in prompt
    assert "申请国家励志奖学金需要提交哪些材料" in prompt
    assert second["data"]["history_pairs"] == 1


def test_history_is_explicitly_not_citable(client, student_headers, seeded_policy,
                                           llm_on, mock_llm):
    """Prompt 必须写清"历史不作引用依据"。

    不写这一句时，模型很愿意把上一轮的结论直接搬到这一轮，
    而那一轮的数字可能来自另一份文件——答案看着有出处，出处其实是它自己。
    """
    llm_on()
    mock_llm(ANSWER)
    conversation_id = ask(
        client, student_headers, "申请国家励志奖学金需要提交哪些材料？"
    ).get_json()["data"]["conversation_id"]
    ask(client, student_headers, "那成绩单呢？", conversation_id=conversation_id)

    prompt = mock_llm.calls["prompt"]
    assert "不能作为引用依据" in prompt
    assert "引用编号只能来自本轮" in prompt


def test_history_can_be_turned_off(client, student_headers, seeded_policy,
                                   llm_on, mock_llm):
    """评测集里每条问题是独立的：带上历史会污染结果，必须能关。"""
    llm_on()
    mock_llm(ANSWER)
    conversation_id = ask(
        client, student_headers, "申请国家励志奖学金需要提交哪些材料？"
    ).get_json()["data"]["conversation_id"]

    body = ask(client, student_headers, "那成绩单呢？",
               conversation_id=conversation_id, use_history=False).get_json()
    assert body["code"] == 0, body
    assert "【对话历史】" not in mock_llm.calls["prompt"]
    assert body["data"]["history_pairs"] == 0


def test_long_answers_are_truncated_in_history(client, student_headers, seeded_policy,
                                               llm_on, mock_llm):
    """旧答案按 240 字截断：证据块不该被历史挤掉，证据才是唯一可引用的东西。"""
    llm_on()
    tail_marker = "尾巴上的这句话不应该出现在后续 Prompt 里"
    # 30 遍 × 10 字 = 300 字，超过 240 的截断线；20 遍只有 200 字，测不出截断
    long_answer = "这一条说明写得很长，" * 30 + tail_marker
    mock_llm('{"answer":"%s [1]","cited":[1],"sufficient":true,"confidence":"high"}'
             % long_answer.replace('"', ""))

    conversation_id = ask(
        client, student_headers, "申请国家励志奖学金需要提交哪些材料？"
    ).get_json()["data"]["conversation_id"]
    ask(client, student_headers, "那成绩单呢？", conversation_id=conversation_id)

    prompt = mock_llm.calls["prompt"]
    assert "这一条说明写得很长" in prompt       # 历史进去了
    assert tail_marker not in prompt            # 但被截断了
    assert "…" in prompt


def test_history_is_scoped_to_the_conversation(client, student_headers, seeded_policy,
                                               llm_on, mock_llm):
    """另一个会话的历史不能串进来：串了就是跨用户的上下文污染。"""
    llm_on()
    mock_llm(ANSWER)
    other = ask(client, student_headers, "奖学金什么时候截止？").get_json()["data"]

    fresh = ask(client, student_headers, "申请国家励志奖学金需要提交哪些材料？").get_json()
    assert fresh["data"]["conversation_id"] != other["conversation_id"]
    prompt = mock_llm.calls["prompt"]
    assert "奖学金什么时候截止" not in prompt
