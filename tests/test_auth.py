"""M2 验收：登录、令牌与角色守卫。

对应用户故事 US-09（越权返 404 不返 403 的**角色部分**返 403）
与 PRD F-01 验收标准。
"""

from __future__ import annotations

import jwt

from tests.conftest import TEST_SECRET, auth


# ---------- 登录 ----------
def test_login_success_returns_token_and_user(client):
    resp = client.post("/api/v1/auth/login",
                       json={"username": "student", "password": "student123"})
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert data["token"]
    assert data["expires_in"] == 720 * 60
    assert data["user"]["username"] == "student"
    assert data["user"]["role"] == "student"
    # 绝不能把密码哈希带出去
    assert "password" not in str(data["user"]).lower()


def test_login_wrong_password_and_unknown_user_are_indistinguishable(client):
    """不允许通过响应差异枚举账号。"""
    wrong = client.post("/api/v1/auth/login",
                        json={"username": "student", "password": "bad"})
    unknown = client.post("/api/v1/auth/login",
                          json={"username": "ghost", "password": "bad"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.get_json()["code"] == unknown.get_json()["code"] == 3001
    assert wrong.get_json()["message"] == unknown.get_json()["message"]


def test_login_missing_field_is_1001(client):
    resp = client.post("/api/v1/auth/login", json={"username": "student"})
    assert resp.get_json()["code"] == 1001


def test_login_rejects_extra_field(client):
    """契约规范：extra='forbid'，未知字段直接 1001。"""
    resp = client.post("/api/v1/auth/login",
                       json={"username": "student", "password": "student123", "role": "admin"})
    assert resp.get_json()["code"] == 1001


# ---------- /auth/me ----------
def test_me_requires_token(client):
    resp = client.get("/api/v1/auth/me")
    assert resp.status_code == 401
    assert resp.get_json()["code"] == 3001


def test_me_with_valid_token(client, student_headers):
    resp = client.get("/api/v1/auth/me", headers=student_headers)
    assert resp.status_code == 200
    assert resp.get_json()["data"]["role"] == "student"


def test_me_rejects_malformed_authorization_header(client, student_token):
    for header in ("", "Token abc", "Bearer", "Bearer    "):
        resp = client.get("/api/v1/auth/me", headers={"Authorization": header})
        assert resp.get_json()["code"] == 3001, header


def test_me_rejects_token_signed_by_other_secret(client, student_token):
    forged = jwt.encode({"sub": "x", "role": "admin", "exp": 9999999999},
                        "another-secret", algorithm="HS256")
    resp = client.get("/api/v1/auth/me", headers=auth(forged))
    assert resp.get_json()["code"] == 3001


def test_me_rejects_expired_token(client):
    expired = jwt.encode({"sub": "x", "role": "admin", "exp": 1},
                         TEST_SECRET, algorithm="HS256")
    resp = client.get("/api/v1/auth/me", headers=auth(expired))
    assert resp.get_json()["code"] == 3001
    assert "过期" in resp.get_json()["message"]


def test_me_rejects_token_of_deleted_user(client, app):
    from app.models import User, db

    token = client.post("/api/v1/auth/login",
                        json={"username": "student", "password": "student123"}
                        ).get_json()["data"]["token"]
    with app.app_context():
        user = db.session.execute(
            db.select(User).where(User.username == "student")
        ).scalar_one()
        db.session.delete(user)
        db.session.commit()
    resp = client.get("/api/v1/auth/me", headers=auth(token))
    assert resp.get_json()["code"] == 3001


# ---------- 角色守卫 ----------
def test_role_guard_blocks_student_on_admin_route(app):
    """守卫本身的行为（不依赖 M3 的上传接口就能测）。"""
    from app.auth import admin_required
    from app.contract import ok

    @app.get("/api/v1/_test_admin_only")
    @admin_required
    def admin_only():
        return ok({"ok": True})

    client = app.test_client()
    student = client.post("/api/v1/auth/login",
                          json={"username": "student", "password": "student123"}
                          ).get_json()["data"]["token"]
    admin = client.post("/api/v1/auth/login",
                        json={"username": "admin", "password": "admin123"}
                        ).get_json()["data"]["token"]

    assert client.get("/api/v1/_test_admin_only", headers=auth(student)
                      ).get_json()["code"] == 3002
    assert client.get("/api/v1/_test_admin_only", headers=auth(admin)).status_code == 200


def test_password_hash_is_not_reversible_and_uses_strong_method(app):
    from app.models import User, db

    with app.app_context():
        user = db.session.execute(
            db.select(User).where(User.username == "admin")
        ).scalar_one()
        assert user.password_hash.startswith("pbkdf2:sha256:")
        assert "admin123" not in user.password_hash
