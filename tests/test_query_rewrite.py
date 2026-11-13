"""查询改写（R3）的单元测试。

**这个模块的第一职责是"不改写也要能用"**，所以下面一半用例在测降级：
没配模型、上游超时、输出不是约定形状、输出缺 `standalone_query`——
四种情况都必须**退回原查询且不抛异常**。任何一条没退回去，
用户看到的就是"点了个追问，整页报错"，而修的是体验缺陷却引入了新缺陷。

另一半点钉在"生效"这条路上：指代词要能还原、扩展词要清洗干净。
特别是 `_clean_terms` 去重与剔除原问题的两条——扩展词若不干净，
第二路召回等于把第一路又跑了一遍，RRF 融合就成了自欺欺人。
"""

from __future__ import annotations

import pytest

from app import llm, rewrite
from app.errors import LlmTimeout, LlmUpstreamError


@pytest.fixture()
def configured(app, monkeypatch):
    """让 `llm.is_configured()` 为真，但不真的发网络请求。"""
    monkeypatch.setattr(llm, "is_configured", lambda: True)
    return app


def _fake_json(payload: dict, *, model: str = "deepseek-flash", latency_ms: int = 812):
    """伪造 `llm.chat_json` 的返回值，并记下它收到的 user prompt。"""
    calls: list[str] = []

    def _inner(system: str, user: str, **_kwargs):
        calls.append(user)
        assert "对话历史" in user and "当前问题" in user
        return payload, llm.ChatResult(content="{}", model=model, latency_ms=latency_ms)

    return _inner, calls


# --- 降级路径 ---------------------------------------------------------------


def test_disabled_by_setting_skips_model(app, monkeypatch):
    """开关关掉时连 is_configured 都不该去看——关闭要真的"零调用"。"""
    monkeypatch.setattr(app.extensions["settings"], "query_rewrite_enabled", False)

    def _boom(*_a, **_k):  # pragma: no cover - 被调用即失败
        raise AssertionError("关闭改写后不应发起任何模型调用")

    monkeypatch.setattr(llm, "chat_json", _boom)
    # `current_settings()` 只在 app 上下文里才读 app 上的实例，测试里要手动推上下文
    with app.app_context():
        result = rewrite.rewrite_query("那它要多久？", [])

    assert result.applied is False
    assert result.attempted is False
    assert result.standalone_query == "那它要多久？"
    assert "QUERY_REWRITE_ENABLED" in result.reason


def test_without_model_falls_back(app, monkeypatch):
    """没配 Key：链路照常，用原查询检索（这是"无模型也能查原文"的底线）。"""
    monkeypatch.setattr(llm, "is_configured", lambda: False)
    result = rewrite.rewrite_query("拿奖学金要准备啥材料", [])

    assert result.applied is False
    assert result.attempted is False
    assert result.query == "拿奖学金要准备啥材料"
    assert "未配置模型" in result.reason


@pytest.mark.parametrize("exc", [
    LlmTimeout("模型响应超时（>60s），可调大 LLM_TIMEOUT_SECONDS"),
    LlmUpstreamError("上游返回 503"),
])
def test_api_error_falls_back_but_marks_attempted(configured, monkeypatch, exc):
    """失败必须**标记 attempted=True**：这次确实花了时间，轨迹里不能当没发生。"""
    monkeypatch.setattr(llm, "chat_json", lambda *_a, **_k: (_ for _ in ()).throw(exc))
    result = rewrite.rewrite_query("那它要多久？", [])

    assert result.applied is False
    assert result.attempted is True, "失败的调用也是调用，否则 model_calls 统计会少一次"
    assert result.query == "那它要多久？"
    assert "已回退原查询" in result.reason


def test_unexpected_exception_does_not_escape(configured, monkeypatch):
    """任何意外异常都不许冒泡——改写在链路中是"可选项"，不能变成单点故障。"""
    monkeypatch.setattr(llm, "chat_json", lambda *_a, **_k: (_ for _ in ()).throw(ValueError("坏了")))
    result = rewrite.rewrite_query("那它要多久？", [])

    assert result.applied is False and result.attempted is True
    assert "ValueError" in result.reason


def test_missing_standalone_query_discards_whole_result(configured, monkeypatch):
    """形状不对就整条弃用：只取一半（有扩展词、没自包含查询）比不改写更难排查。"""
    fake, _ = _fake_json({"expanded_terms": ["国家奖学金", "评审办法"]})
    monkeypatch.setattr(llm, "chat_json", fake)
    result = rewrite.rewrite_query("那它要多久？", [])

    assert result.applied is False
    assert result.expanded_terms == []
    assert result.query == "那它要多久？"
    assert "standalone_query" in result.reason


def test_blank_standalone_query_is_not_applied(configured, monkeypatch):
    fake, _ = _fake_json({"standalone_query": "   ", "expanded_terms": ["奖学金"]})
    monkeypatch.setattr(llm, "chat_json", fake)
    result = rewrite.rewrite_query("那它要多久？", [])

    assert result.applied is False
    assert result.expanded_terms == []


# --- 生效路径 ---------------------------------------------------------------


def test_pronoun_is_resolved(configured, monkeypatch):
    fake, calls = _fake_json({
        "standalone_query": "国家励志奖学金从评审结果公示到发放需要多长时间",
        "expanded_terms": ["国家励志奖学金", "发放时间", "评审结果公示"],
    })
    monkeypatch.setattr(llm, "chat_json", fake)
    history = [
        {"role": "user", "content": "国家励志奖学金什么时候发放"},
        {"role": "assistant", "content": "评审结果公示后于 11 月 30 日前发放。"},
    ]
    result = rewrite.rewrite_query("那它要多久？", history)

    assert result.applied is True
    assert result.attempted is True
    assert result.query.startswith("国家励志奖学金")
    assert result.expanded_terms == ["国家励志奖学金", "发放时间", "评审结果公示"]
    assert result.model == "deepseek-flash" and result.latency_ms == 812
    # 历史确实进了 prompt，且带上角色标记
    assert "用户：国家励志奖学金什么时候发放" in calls[0]
    assert "助手：评审结果公示后" in calls[0]


def test_no_history_uses_explicit_placeholder(configured, monkeypatch):
    """首轮没有历史时写"（无…）"而不是空行：空行容易被模型理解成格式错了。"""
    fake, calls = _fake_json({"standalone_query": "申请奖学金需要哪些材料"})
    monkeypatch.setattr(llm, "chat_json", fake)
    rewrite.rewrite_query("拿奖学金要准备啥材料", [])

    assert rewrite.NO_HISTORY in calls[0]


def test_terms_are_cleaned(configured, monkeypatch):
    """去空、去重、剔除与原问题字面相同的词（否则第二路=第一路的复读）。

    注意 `expanded_terms` 里放了一个整句等于原问题的词条：它必须被剔除，
    判定口径是"与 query 去空白后逐字相等"。
    """
    fake, _ = _fake_json({
        "standalone_query": "申请奖学金需要准备哪些材料",
        "expanded_terms": ["  奖学金  ", "", "奖学金", "拿奖学金要准备啥材料",
                           "申请材料", 123, "评审办法"],
    })
    monkeypatch.setattr(llm, "chat_json", fake)
    result = rewrite.rewrite_query("拿奖学金要准备啥材料", [])

    assert result.expanded_terms == ["奖学金", "申请材料", "评审办法"]
    assert result.reason is None


def test_terms_are_capped(configured, monkeypatch):
    """超过 QUERY_REWRITE_MAX_TERMS 的词条一律截断——上限是对第二路的成本约束。"""
    fake, _ = _fake_json({
        "standalone_query": "申请奖学金需要准备哪些材料",
        "expanded_terms": ["a1", "a2", "a3", "a4", "a5", "a6", "a7"],
    })
    monkeypatch.setattr(llm, "chat_json", fake)
    result = rewrite.rewrite_query("拿奖学金要准备啥材料", [])

    assert len(result.expanded_terms) == 5


def test_empty_terms_still_applied_but_flagged(configured, monkeypatch):
    """没给扩展词也算生效（自包含查询本身就够用），但 reason 要留痕。"""
    fake, _ = _fake_json({"standalone_query": "国家励志奖学金发放时间", "expanded_terms": []})
    monkeypatch.setattr(llm, "chat_json", fake)
    result = rewrite.rewrite_query("那它要多久？", [])

    assert result.applied is True
    assert result.expanded_terms == []
    assert "未给出扩展词" in result.reason


# --- 历史压缩 ---------------------------------------------------------------


def test_format_history_truncates_each_message():
    long_answer = "答" * 400
    text = rewrite.format_history([
        {"role": "user", "content": "问题一"},
        {"role": "assistant", "content": long_answer},
    ])

    lines = text.split("\n")
    assert lines[0] == "用户：问题一"
    # 240 字 + 省略号；助手回答动辄几百字，不截断会把当前问题挤出 Prompt 的注意力范围
    assert lines[1] == "助手：" + "答" * rewrite.HISTORY_MSG_MAX_CHARS + "…"


def test_format_history_flattens_newlines():
    """换行要压成空格：否则一条多段回答会在 prompt 里伪装成好几轮对话。"""
    text = rewrite.format_history([{"role": "assistant", "content": "第一行\n第二行"}])
    assert text == "助手：第一行 第二行"


def test_to_public_shape_is_stable():
    """轨迹里读的字段名必须稳定——前端与文档都按这几个键取值。"""
    keys = set(rewrite.RewriteResult("q").to_public())
    assert keys == {"standalone_query", "expanded_terms", "applied",
                    "attempted", "reason", "model", "latency_ms"}
