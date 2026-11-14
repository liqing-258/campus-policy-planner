"""M2 验收：健康检查。

门禁原文："新环境按 README 能起；`/health` 200；无密钥入库"
本文件覆盖前两条；"无密钥入库"由 tests/test_security_scan.py 覆盖。
"""

from __future__ import annotations

import os


def test_health_returns_200_and_ok(client):
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert data["status"] == "ok"
    assert data["db"] == "ok"


def test_health_reports_degraded_mode_without_api_key(client):
    """没有 LLM_API_KEY 时必须自报 degraded，不允许伪装成模型已生效。"""
    data = client.get("/api/v1/health").get_json()["data"]
    assert data["llm_mode"] == "degraded"
    assert data["llm_model"] is None
    assert data["embedding_mode"] == "disabled"


def test_health_never_leaks_secrets(client):
    """健康检查是公开接口，绝不能回显令牌/密钥**的值**。

    R6 起这条断言明确盯「值」而不是「词」：`llm_config` 为了可诊断会带上
    `api_key: "set" | "missing"` 和 `missing: ["LLM_API_KEY"]`——字段名与变量名
    都必须出现，否则用户没法知道自己缺什么。真正不允许的是把值吐出来。
    所以这里比对具体密钥，并保留几个纯敏感词（口令、令牌）的字面检查。
    """
    resp = client.get("/api/v1/health")
    raw = resp.get_data(as_text=True)
    lowered = raw.lower()
    for forbidden in ("jwt_secret", "password", "token", "bearer"):
        assert forbidden not in lowered, f"/health 泄露了 {forbidden}"

    real_key = os.getenv("LLM_API_KEY", "")
    if real_key:
        assert real_key not in raw, "/health 回显了真实密钥"
    cfg = resp.get_json()["data"]["llm_config"]
    assert set(cfg) == {
        "configured", "api_key", "model", "base_url", "missing", "env_files",
    }, "字段集变了就要重审：新增字段可能带上不该公开的值"


def test_health_counts_are_zero_on_empty_database(client):
    data = client.get("/api/v1/health").get_json()["data"]
    assert data["document_count"] == 0
    assert data["chunk_count"] == 0


def test_root_route_serves_the_built_frontend_or_a_json_hint(client):
    """根路径：前端已构建就托管页面，未构建就退化为 JSON 说明。

    这条测试不能写死其中一种形态——CI 上可能没跑 `npm run build`，
    而打包版一定有 dist。两种都算通过，判据是"不能 404、不能 500"。
    """
    resp = client.get("/")
    assert resp.status_code == 200
    if resp.is_json:
        assert resp.get_json()["api"] == "/api/v1"
    else:
        assert b'<div id="app">' in resp.data


def test_unknown_api_path_is_json_404_not_the_spa(client):
    """`/api/*` 的 404 必须回 JSON。

    如果把 index.html 回给前端，`fetch(...).json()` 会报"解析失败"，
    真正的原因（接口路径写错）反而被埋掉。
    """
    resp = client.get("/api/v1/no-such-endpoint")
    assert resp.status_code == 404
    assert resp.is_json
