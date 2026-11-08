"""M2 验收：健康检查。

门禁原文："新环境按 README 能起；`/health` 200；无密钥入库"
本文件覆盖前两条；"无密钥入库"由 tests/test_security_scan.py 覆盖。
"""

from __future__ import annotations


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
    """健康检查是公开接口，绝不能回显令牌/密钥。"""
    raw = client.get("/api/v1/health").get_data(as_text=True).lower()
    for forbidden in ("jwt_secret", "api_key", "password", "token", "bearer"):
        assert forbidden not in raw, f"/health 泄露了 {forbidden}"


def test_health_counts_are_zero_on_empty_database(client):
    data = client.get("/api/v1/health").get_json()["data"]
    assert data["document_count"] == 0
    assert data["chunk_count"] == 0


def test_root_route_is_human_readable(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.get_json()["api"] == "/api/v1"
