"""响应外壳与通用约定的测试。

这些用例保护的是"契约一致性"，修改 contract.py 时它们会立刻报警。
"""

from __future__ import annotations

import re

ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
ISO_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

ENVELOPE_KEYS = ["code", "message", "data", "request_id", "timestamp"]


def test_envelope_key_order_and_types(client):
    body = client.get("/api/v1/health").get_json()
    assert list(body.keys()) == ENVELOPE_KEYS
    assert body["code"] == 0
    assert body["message"] == "success"
    assert isinstance(body["data"], dict)
    assert ULID_RE.match(body["request_id"]), body["request_id"]
    assert ISO_UTC_RE.match(body["timestamp"]), body["timestamp"]


def test_request_id_is_unique_per_request(client):
    ids = {client.get("/api/v1/health").get_json()["request_id"] for _ in range(5)}
    assert len(ids) == 5


def test_request_id_echoed_in_header(client):
    resp = client.get("/api/v1/health")
    assert resp.headers["X-Request-Id"] == resp.get_json()["request_id"]


def test_inbound_request_id_is_respected(client):
    resp = client.get("/api/v1/health", headers={"X-Request-Id": "trace-from-gateway"})
    assert resp.get_json()["request_id"] == "trace-from-gateway"


def test_error_keeps_envelope_and_null_data(client):
    body = client.post("/api/v1/auth/login", json={"username": "student"}).get_json()
    assert list(body.keys()) == ENVELOPE_KEYS
    assert body["code"] == 1001
    assert body["data"] is None
    assert body["message"].startswith("参数校验失败")


def test_unknown_route_returns_2002_json_not_html(client):
    resp = client.get("/api/v1/does-not-exist")
    assert resp.status_code == 404
    body = resp.get_json()
    assert body["code"] == 2002
    assert body["data"] is None


def test_wrong_method_returns_405_json(client):
    resp = client.delete("/api/v1/health")
    assert resp.status_code == 405
    assert resp.get_json()["code"] == 1001


def test_invalid_json_body_is_1001_not_500(client):
    resp = client.post("/api/v1/auth/login", data="{not json",
                       content_type="application/json")
    assert resp.status_code == 400
    assert resp.get_json()["code"] == 1001


def test_oversized_body_returns_1004(app, client):
    # MAX_CONTENT_LENGTH 在进入视图前生效，必须映射成 1004 而不是 500
    limit = app.config["MAX_CONTENT_LENGTH"]
    resp = client.post("/api/v1/auth/login", data=b"x" * (limit + 1024),
                       content_type="application/json")
    assert resp.status_code == 413
    assert resp.get_json()["code"] == 1004


def test_chinese_not_escaped_in_response(client):
    body = client.post("/api/v1/auth/login", json={"username": "无人", "password": "x"})
    assert "用户名或密码错误" in body.get_data(as_text=True)
