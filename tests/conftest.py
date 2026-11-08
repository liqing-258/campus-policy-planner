"""测试夹具。

要点：
- 每个测试用例用**独立的临时 SQLite 文件**，互不污染，也不碰开发库 data/app.db
- 上传目录同样隔离，避免测试残留文件混进演示数据
- 账号由夹具直接写入（不走 tools/seed_demo.py），保证测试不依赖脚本执行顺序
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from app.auth import hash_password  # noqa: E402
from app.contract import new_ulid  # noqa: E402
from app.models import User, db  # noqa: E402
from shared.enums import ROLE_ADMIN, ROLE_STUDENT  # noqa: E402

TEST_SECRET = "test-only-secret-0123456789abcdef"


@pytest.fixture()
def app(tmp_path):
    instance = create_app({
        "database_url": f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        "upload_dir": str(tmp_path / "uploads"),
        "data_dir": str(tmp_path),
        "jwt_secret": TEST_SECRET,
        "app_env": "test",
        "llm_api_key": "",
        "llm_model": "",
        "embedding_api_key": "",
        "embedding_model": "",
    })
    with instance.app_context():
        db.create_all()
        db.session.add_all([
            User(id=new_ulid(), username="student", display_name="演示学生",
                 password_hash=hash_password("student123"), role=ROLE_STUDENT),
            User(id=new_ulid(), username="admin", display_name="演示管理员",
                 password_hash=hash_password("admin123"), role=ROLE_ADMIN),
        ])
        db.session.commit()
    yield instance


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def student_token(client):
    resp = client.post("/api/v1/auth/login",
                       json={"username": "student", "password": "student123"})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()["data"]["token"]


@pytest.fixture()
def admin_token(client):
    resp = client.post("/api/v1/auth/login",
                       json={"username": "admin", "password": "admin123"})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()["data"]["token"]


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def student_headers(student_token):
    return auth(student_token)


@pytest.fixture()
def admin_headers(admin_token):
    return auth(admin_token)
