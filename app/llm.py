"""模型适配器（OpenAI 兼容协议）。

设计要点，全部来自 M4 实测而非推测：

1. **推理模型的空 content 不是"回答为空"，是预算被吃光。**
   实测 `deepseek-flash` 与 `deepseek-v4-pro` 都是推理模型，`usage.completion_tokens_details.reasoning_tokens`
   会先占用 `max_tokens` 预算。max_tokens=100 时实测 reasoning 占满 100、content 返回 `''`。
   若不区分，上层会把它当成"模型说没找到依据"，从而误判成拒答——这属于**把故障伪装成业务结论**。
   因此这里显式抛出 LlmOutputInvalid，并在 message 里写清是预算问题。

2. **不在这里做 Prompt 拼装，也不做引用校验。**
   本模块只管"把 messages 发出去、把 JSON 取回来"，业务语义留给 app/qa.py。
   否则将来换检索策略还要改模型层，耦合没必要。

3. **失败要分类，因为四类失败的运维动作完全不同**：
   超时（4004，可调大超时）、上游 5xx/401（4005，检查 Key 与配额）、
   输出非法（4002，改 Prompt 或重试）、未配置（4006，降级）。
   一律报 500 会让评审无法判断到底是哪一环坏了。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

from app.errors import (
    DegradedMode,
    LlmOutputInvalid,
    LlmTimeout,
    LlmUpstreamError,
)
from app.runtime import current_settings

# 模型偶尔会把 JSON 包在 Markdown 代码块里，即使 Prompt 明确要求不要
_FENCE = re.compile(r"^\s*```(?:json|JSON)?\s*\n(.*?)\n?```\s*$", re.S)


@dataclass
class ChatResult:
    """一次模型调用的完整留痕。latency 与 token 数会写进 messages 表。"""

    content: str
    model: str
    latency_ms: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None
    attempts: int = 1
    # 原始响应留一份，便于复现"为什么这条被判非法"——但**不入库**，只在内存里活一个请求周期
    raw: dict = field(default_factory=dict, repr=False)


def is_configured() -> bool:
    """Key 与模型名都有才算配好。只有 base_url 不算——那是模板默认值，不是用户填的。"""
    settings = current_settings()
    return bool(settings.llm_api_key and settings.llm_model)


def model_name() -> str:
    return current_settings().llm_model or ""


def _payload(messages: list[dict], *, temperature: float | None,
             max_tokens: int | None, json_mode: bool) -> dict:
    settings = current_settings()
    body: dict = {
        "model": settings.llm_model,
        "messages": messages,
        "temperature": settings.llm_temperature if temperature is None else temperature,
        "max_tokens": settings.llm_max_tokens if max_tokens is None else max_tokens,
        "stream": False,
    }
    if json_mode:
        # 实测 DeepSeek 支持该参数；不支持的供应商会自动忽略未知字段，不会报错
        body["response_format"] = {"type": "json_object"}
    return body


def _post(body: dict) -> tuple[dict, int]:
    """发请求。独立成函数，测试里直接 monkeypatch 它即可，无需起假服务器。"""
    import httpx

    settings = current_settings()
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    started = time.perf_counter()
    try:
        resp = httpx.post(
            url,
            json=body,
            headers={
                "Authorization": f"Bearer {settings.llm_api_key}",
                "Content-Type": "application/json",
            },
            timeout=settings.llm_timeout_seconds,
        )
    except httpx.TimeoutException:
        raise LlmTimeout(
            f"模型响应超时（>{settings.llm_timeout_seconds}s），可调大 LLM_TIMEOUT_SECONDS"
        )
    except httpx.HTTPError as exc:
        raise LlmUpstreamError(f"无法连接模型服务：{type(exc).__name__}")

    elapsed_ms = int((time.perf_counter() - started) * 1000)

    if resp.status_code == 401:
        raise LlmUpstreamError("模型服务鉴权失败（401），请检查 LLM_API_KEY 是否正确")
    if resp.status_code == 404:
        raise LlmUpstreamError(
            f"模型接口返回 404，请检查 LLM_BASE_URL 是否需带 /v1，以及 LLM_MODEL={settings.llm_model!r} 是否存在"
        )
    if resp.status_code >= 500:
        raise LlmUpstreamError(f"模型服务返回 {resp.status_code}")
    if resp.status_code >= 400:
        # 400 多发于模型名不存在，把上游原文带给调用方，比"请求失败"有用得多
        detail = resp.text[:200]
        raise LlmUpstreamError(f"模型服务拒绝请求（{resp.status_code}）：{detail}")

    try:
        data = resp.json()
    except Exception:
        raise LlmOutputInvalid("模型服务返回的不是 JSON")
    return data, elapsed_ms


def chat(messages: list[dict], *, temperature: float | None = None,
         max_tokens: int | None = None, json_mode: bool = True,
         _retry: int | None = None) -> ChatResult:
    """单次对话。未配置模型时抛 DegradedMode，由调用方决定回退到什么。"""
    if not is_configured():
        raise DegradedMode()

    settings = current_settings()
    retries = settings.llm_max_retry if _retry is None else _retry
    last_error: Exception | None = None

    for attempt in range(1, retries + 2):
        try:
            data, elapsed_ms = _post(_payload(
                messages, temperature=temperature, max_tokens=max_tokens, json_mode=json_mode))
            return _to_result(data, elapsed_ms, attempt, messages)
        except LlmTimeout:
            # 超时不重试：再试一次大概率还是超时，白白多等一个 timeout
            raise
        except (LlmOutputInvalid, LlmUpstreamError) as exc:
            last_error = exc
            if not getattr(exc, "retriable", True) or attempt > retries:
                raise
    raise last_error  # pragma: no cover - 循环必在内部 raise


def _to_result(data: dict, elapsed_ms: int, attempt: int, messages: list[dict]) -> ChatResult:
    settings = current_settings()
    try:
        choice = data["choices"][0]
        message = choice.get("message") or {}
    except (KeyError, IndexError, TypeError):
        raise LlmOutputInvalid("模型响应缺少 choices[0].message")

    content = message.get("content")
    usage = data.get("usage") or {}
    details = usage.get("completion_tokens_details") or {}
    reasoning = details.get("reasoning_tokens")

    if content is None:
        content = ""
    if not str(content).strip():
        # 见模块文档第 1 条：这是**预算不足**的典型症状，不是"模型无话可说"
        if reasoning:
            budget = settings.llm_max_tokens
            raise LlmOutputInvalid(
                f"模型只输出了推理内容（reasoning_tokens={reasoning}），正文为空——"
                f"max_tokens={budget} 被推理耗尽，请调大 LLM_MAX_TOKENS",
                retriable=False,
            )
        raise LlmOutputInvalid(
            f"模型返回正文为空（finish_reason={choice.get('finish_reason')!r}）"
        )

    return ChatResult(
        content=str(content),
        model=data.get("model") or settings.llm_model,
        latency_ms=elapsed_ms,
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        reasoning_tokens=reasoning,
        attempts=attempt,
        raw=data,
    )


def strip_fence(text: str) -> str:
    """去掉 ```json 围栏。模型常无视"不要用代码块"的指令，属于已知常态。"""
    matched = _FENCE.match(text)
    return matched.group(1) if matched else text.strip()


def chat_json(system: str, user: str, *, temperature: float | None = None,
              max_tokens: int | None = None) -> tuple[dict, ChatResult]:
    """要求模型返回 JSON 对象，并在这里完成"是不是合法 JSON 对象"的第一道校验。

    注意分工：**这里只保证"是 JSON 对象"**；字段是否齐备、类型对不对，
    由 app/qa.py 用 pydantic 校验（N-01 契约校验闸）。分两层是因为
    "不是 JSON"要靠重试解决，"字段缺失"要靠 Prompt 修——处理方式不同。

    重试放在这一层而不是 chat()：因为"不是合法 JSON"只有在解析时才知道，
    chat() 看不到。所以这里用 `_retry=0` 调 chat（单次），自己管总次数。
    """
    settings = current_settings()
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    retries = settings.llm_max_retry
    last_error: Exception | None = None

    for attempt in range(1, retries + 2):
        # _retry=0 → chat 内不再重试；timeout/upstream 仍按自身语义抛出
        result = chat(messages, temperature=temperature, max_tokens=max_tokens,
                      json_mode=True, _retry=0)
        result.attempts = attempt
        text = strip_fence(result.content)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            last_error = LlmOutputInvalid(
                f"模型输出不是合法 JSON（{exc.msg} at pos {exc.pos}）", retriable=True)
        else:
            if isinstance(parsed, dict):
                return parsed, result
            last_error = LlmOutputInvalid(
                f"模型输出应为 JSON 对象，实际为 {type(parsed).__name__}", retriable=True)

        if attempt > retries:
            raise last_error

    raise last_error  # pragma: no cover - 循环必在内部 raise
