"""R15 政策更新提醒：预检、版本关联、历史版本、更新列表、匹配提醒。

这一层的断言都打在**具体字段与具体文案**上，重点防三种"看起来对"：
① 自动把没确认的文件标成「现行有效」；② 版本环；③ 编造条款差异
（更新卡只许有事实字段，`change_summary` 只许是两句固定话）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.contract import new_ulid
from app.models import (
    ActionPlan,
    AuditLog,
    Document,
    DocumentChunk,
    PolicyRule,
    User,
    db,
)

DEMO = Path(__file__).resolve().parent.parent / "demo"
PDF_OK = DEMO / "01-本科生奖学金评审办法（2026年版）.pdf"
DOCX_OK = DEMO / "02-学生事务办理指南.docx"


def _login(client, username="student", password="student123") -> dict:
    resp = client.post("/api/v1/auth/login",
                       json={"username": username, "password": password})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return {"Authorization": f"Bearer {resp.get_json()['data']['token']}"}


def _student(app) -> User:
    return db.session.execute(
        db.select(User).where(User.username == "student")
    ).scalars().first()


def _doc(app, title: str, *, effective: str = "unknown",
         published: str | None = None, created: datetime | None = None,
         supersedes: str | None = None) -> str:
    with app.app_context():
        from datetime import date
        doc = Document(
            id=new_ulid(), title=title, file_name=f"{title}.pdf",
            stored_name=f"{new_ulid()}.pdf", file_type="pdf", file_size=1024,
            file_hash=new_ulid(), effective_status=effective,
            published_at=date.fromisoformat(published) if published else None,
            supersedes_id=supersedes,
            parse_status="indexed", uploader_id=_student(app).id,
        )
        if created is not None:
            doc.created_at = created
            doc.updated_at = created
        db.session.add(doc)
        db.session.commit()
        return doc.id


def _plan(app, document_id: str, title: str = "我的申请计划") -> str:
    with app.app_context():
        plan = ActionPlan(
            id=new_ulid(), user_id=_student(app).id, document_id=document_id,
            title=title, status="confirmed",
        )
        db.session.add(plan)
        db.session.commit()
        return plan.id


def _rule(app, document_id: str, quote: str = "申请人须为在籍本科生。") -> str:
    with app.app_context():
        chunk = DocumentChunk(
            id=new_ulid(), document_id=document_id, chunk_index=0,
            content=quote, char_start=0, char_end=len(quote), token_count=10,
        )
        db.session.add(chunk)
        db.session.commit()
        rule = PolicyRule(
            id=new_ulid(), document_id=document_id, chunk_id=chunk.id,
            rule_code=f"R{new_ulid()[:6]}", rule_type="eligibility",
            description="测试条件", expected_value=quote,
            original_quote=quote, quote_verified=True,
            extraction_status="verified", review_status="approved",
        )
        db.session.add(rule)
        db.session.commit()
        return rule.id


def upload(client, headers, path: Path, **form):
    with open(path, "rb") as handle:
        data = {"file": (handle, path.name)}
        data.update(form)
        return client.post("/api/v1/documents", headers=headers, data=data,
                           content_type="multipart/form-data")


# ================================================================ 预检
def test_candidates_strong_when_same_topic(app, client, admin_headers):
    _doc(app, "本科生奖学金评审办法（2025年版）", published="2025-09-01")
    resp = client.get(
        "/api/v1/documents/version-candidates?title=本科生奖学金评审办法（2026年版）",
        headers=admin_headers)
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert len(data["strong"]) == 1
    assert data["strong"][0]["match"] == "same_topic"
    assert data["weak"] == []


def test_candidates_weak_when_similar_title(app, client, admin_headers):
    _doc(app, "全国大学英语四六级考试报名须知")
    resp = client.get(
        "/api/v1/documents/version-candidates?title=全国大学英语四六级考试报名通知",
        headers=admin_headers)
    data = resp.get_json()["data"]
    assert data["strong"] == []
    assert len(data["weak"]) == 1
    assert data["weak"][0]["match"] == "similar"
    assert data["weak"][0]["similarity"] >= 0.55


def test_candidates_ignore_unrelated_title(app, client, admin_headers):
    _doc(app, "图书馆借阅与自习空间管理办法")
    resp = client.get(
        "/api/v1/documents/version-candidates?title=国家助学金评审实施细则",
        headers=admin_headers)
    data = resp.get_json()["data"]
    assert data["strong"] == [] and data["weak"] == []


def test_candidates_requires_admin_and_title(app, client, admin_headers):
    student = _login(client)
    resp = client.get("/api/v1/documents/version-candidates?title=某某政策",
                      headers=student)
    assert resp.get_json()["code"] == 3002
    resp = client.get("/api/v1/documents/version-candidates", headers=admin_headers)
    assert resp.get_json()["code"] == 1001


def test_upload_response_carries_possible_updates(client, admin_headers):
    """上传同主题文件时响应即带候选——"上传时提示"的落点。

    两个文件各传一次（同 hash 会被 2001 拦），标题造出同主题。
    """
    first = upload(client, admin_headers, DOCX_OK,
                   title="学生事务办理指南（2026年版）")
    assert first.status_code == 201
    assert first.get_json()["data"]["possible_updates"]["strong"] == []
    second = upload(client, admin_headers, PDF_OK,
                    title="学生事务办理指南（2027年版）")
    assert second.status_code == 201
    updates = second.get_json()["data"]["possible_updates"]
    assert [u["title"] for u in updates["strong"]] == ["学生事务办理指南（2026年版）"]
    # 上传本身不改任何人的有效状态（不得自动标现行有效）
    assert second.get_json()["data"]["effective_status"] == "unknown"


# ================================================================ 版本关联
def test_version_link_marks_old_superseded_and_new_active(app, client, admin_headers):
    old = _doc(app, "国家奖学金评审办法（2025年版）", effective="active",
               published="2025-09-01")
    new = _doc(app, "国家奖学金评审办法（2026年版）", effective="unknown",
               published="2026-09-01")
    resp = client.post(f"/api/v1/documents/{new}/version-link",
                       json={"supersedes_id": old}, headers=admin_headers)
    assert resp.status_code == 200
    body = resp.get_json()["data"]
    assert body["document"]["supersedes_id"] == old
    assert body["document"]["effective_status"] == "active"   # 管理员确认 → 相应状态
    assert body["superseded"]["effective_status"] == "superseded"


def test_version_link_never_activates_without_explicit_confirm(app, client, admin_headers):
    """默认 active，但管理员显式传 unknown 就**不**标现行有效。"""
    old = _doc(app, "校级优秀学生评选办法（2024年版）")
    new = _doc(app, "校级优秀学生评选办法（2025年版）")
    resp = client.post(f"/api/v1/documents/{new}/version-link",
                       json={"supersedes_id": old, "new_status": "unknown"},
                       headers=admin_headers)
    assert resp.get_json()["data"]["document"]["effective_status"] == "unknown"


def test_version_link_rejects_self_and_missing(app, client, admin_headers):
    doc_id = _doc(app, "勤工助学岗位管理办法（2026年版）")
    bad = client.post(f"/api/v1/documents/{doc_id}/version-link",
                      json={"supersedes_id": doc_id}, headers=admin_headers)
    assert bad.get_json()["code"] == 1001
    missing = client.post(f"/api/v1/documents/{doc_id}/version-link",
                          json={"supersedes_id": "01HX000000000000000000000A"},
                          headers=admin_headers)
    assert missing.get_json()["code"] == 2002


def test_version_link_rejects_cycle(app, client, admin_headers):
    a = _doc(app, "学生违纪处分办法（2023年版）")
    b = _doc(app, "学生违纪处分办法（2024年版）")
    ok = client.post(f"/api/v1/documents/{b}/version-link",
                     json={"supersedes_id": a}, headers=admin_headers)
    assert ok.status_code == 200
    cyc = client.post(f"/api/v1/documents/{a}/version-link",
                      json={"supersedes_id": b}, headers=admin_headers)
    assert cyc.get_json()["code"] == 1001
    assert "版本环" in cyc.get_json()["message"]


def test_version_link_requires_admin_and_writes_audit(app, client, admin_headers):
    old = _doc(app, "实验室安全准入管理办法（2025年版）")
    new = _doc(app, "实验室安全准入管理办法（2026年版）")
    student = _login(client)
    denied = client.post(f"/api/v1/documents/{new}/version-link",
                         json={"supersedes_id": old}, headers=student)
    assert denied.get_json()["code"] == 3002
    client.post(f"/api/v1/documents/{new}/version-link",
                json={"supersedes_id": old}, headers=admin_headers)
    with app.app_context():
        row = db.session.execute(
            db.select(AuditLog).where(AuditLog.action == "document.version-link")
        ).scalars().first()
        assert row is not None and row.resource_id == new


# ================================================================ 历史版本
def test_versions_endpoint_lists_family(app, client, admin_headers):
    old = _doc(app, "国家励志奖学金评审办法（2025年版）", effective="superseded",
               published="2025-09-01")
    new = _doc(app, "国家励志奖学金评审办法（2026年版）", effective="active",
               published="2026-09-01", supersedes=old)
    resp = client.get(f"/api/v1/documents/{old}/versions", headers=admin_headers)
    data = resp.get_json()["data"]
    assert data["count"] == 2
    assert data["newest_document_id"] == new
    assert [v["document_id"] for v in data["versions"]] == [new, old]
    assert [v["is_current"] for v in data["versions"]] == [False, True]
    assert "现行有效" in data["hint"]


def test_versions_chain_includes_renamed_titles(app, client, admin_headers):
    """标题改过名（主题比对不上）但人工建了链 → 必须进同一家族。"""
    old = _doc(app, "关于做好毕业生图像信息采集的通知")
    new = _doc(app, "关于做好毕业生图像信息采集工作的通知（新版）", supersedes=old)
    resp = client.get(f"/api/v1/documents/{new}/versions", headers=admin_headers)
    ids = {v["document_id"] for v in resp.get_json()["data"]["versions"]}
    assert ids == {old, new}


def test_versions_hint_reports_ambiguous_active(app, client, admin_headers):
    a = _doc(app, "本科生转专业管理办法（2025年版）", effective="active")
    b = _doc(app, "本科生转专业管理办法（2026年版）", effective="active")
    resp = client.get(f"/api/v1/documents/{a}/versions", headers=admin_headers)
    assert "冲突" in resp.get_json()["data"]["hint"]


def test_replacer_wins_over_date_guess(app, client, admin_headers):
    """人工链优先于日期推断：旧版被标「已被替代」后，newest 必须是替代它的那份。

    第一版按日期排序取 newest——无发布日期但年份大的测试文件把 2026 版压下去，
    横幅因此认为"当前就是最新版"，管理员确认过的事实反而不显示。
    """
    old = _doc(app, "学生住宿管理规定（2025年版）", effective="superseded",
               published="2025-01-01")
    new = _doc(app, "学生住宿管理规定（2026年版）", effective="active",
               published="2026-01-01", supersedes=old)
    tricky = _doc(app, "学生住宿管理规定（2099年版）")  # 无发布日期、年份却最大
    resp = client.get(f"/api/v1/documents/{old}/versions", headers=admin_headers)
    data = resp.get_json()["data"]
    assert data["newest_document_id"] == new      # 不是 tricky
    assert data["count"] == 3


def test_replacer_wins_over_date_guess(app, client, admin_headers):
    """人工链优先于日期推断：旧版被标「已被替代」后，newest 必须是替代它的那份。

    第一版按日期排序取 newest——无发布日期但年份大的测试文件把 2026 版压下去，
    横幅因此认为"当前就是最新版"，管理员确认过的事实反而不显示。
    """
    old = _doc(app, "学生住宿管理规定（2025年版）", effective="superseded",
               published="2025-01-01")
    new = _doc(app, "学生住宿管理规定（2026年版）", effective="active",
               published="2026-01-01", supersedes=old)
    tricky = _doc(app, "学生住宿管理规定（2099年版）")  # 无发布日期、年份却最大
    resp = client.get(f"/api/v1/documents/{old}/versions", headers=admin_headers)
    data = resp.get_json()["data"]
    assert data["newest_document_id"] == new      # 不是 tricky
    assert data["count"] == 3


def test_document_payload_carries_supersedes_id(app, client, admin_headers):
    old = _doc(app, "教材选用管理办法（2024年版）")
    new = _doc(app, "教材选用管理办法（2025年版）", supersedes=old)
    resp = client.get(f"/api/v1/documents/{new}", headers=admin_headers)
    assert resp.get_json()["data"]["supersedes_id"] == old


# ================================================================ 学生端更新提醒
def test_updates_lists_recent_and_flags_plan_relation(app, client):
    fresh = _doc(app, "国家助学金评审实施细则（2026年版）",
                 published="2026-11-01", effective="active")
    _plan(app, fresh, "助学金申请计划")
    headers = _login(client)
    resp = client.get("/api/v1/policies/updates?days=30", headers=headers)
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    item = next(i for i in data["items"] if i["document_id"] == fresh)
    assert item["change_kind"] == "new_file"
    assert item["published_at"] == "2026-11-01"
    assert item["effective_status"] == "active"
    assert item["may_affect_plans"] is True
    assert item["related_plans"][0]["title"] == "助学金申请计划"
    assert data["counts"]["plan_related"] >= 1


def test_updates_never_fabricates_clause_diffs(app, client):
    """需求红线：不能编造条款差异。响应里只许有事实字段与两句固定话。"""
    _doc(app, "学生住宿管理规定（2026年版）")
    headers = _login(client)
    data = client.get("/api/v1/policies/updates", headers=headers).get_json()["data"]
    assert "不比对条款内容" in data["notice"]
    allowed_summaries = {"新文件入库（可能是新版本）", "文件信息有更新（如有效状态调整）"}
    for item in data["items"]:
        assert item["change_summary"] in allowed_summaries
        assert "diff" not in item and "changes" not in item and "差异" not in item
        assert set(item) <= {
            "document_id", "title", "department", "published_at", "academic_year",
            "version_label", "effective_status", "created_at", "updated_at",
            "change_kind", "change_summary", "version_count", "has_newer",
            "newer_document_id", "related_plans", "may_affect_plans",
        }


def test_updates_window_is_respected(app, client):
    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=40)
    _doc(app, "暑期社会实践管理办法（2025年版）", created=old)
    headers = _login(client)
    items = client.get("/api/v1/policies/updates?days=30",
                       headers=headers).get_json()["data"]["items"]
    assert all(i["title"] != "暑期社会实践管理办法（2025年版）" for i in items)


def test_updates_validates_days_and_requires_login(client):
    headers = _login(client)
    for bad in ("0", "91", "abc"):
        resp = client.get(f"/api/v1/policies/updates?days={bad}", headers=headers)
        assert resp.get_json()["code"] == 1001
    assert client.get("/api/v1/policies/updates").get_json()["code"] == 3001


# ================================================================ 匹配提醒
def _miniprofile_match(client, headers, document_id: str) -> dict:
    resp = client.post("/api/v1/matches", headers=headers, json={
        "document_id": document_id,
        "profile": {"student_type": "full_time_undergraduate", "grade": 2,
                    "gpa": 3.8, "disciplinary_record": "none"},
    })
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()["data"]


def test_match_carries_version_note_when_multiple(app, client):
    old = _doc(app, "评奖评优综合测评办法（2025年版）", effective="superseded",
               published="2025-06-01")
    new = _doc(app, "评奖评优综合测评办法（2026年版）", effective="active",
               published="2026-06-01", supersedes=old)
    _rule(app, new)
    headers = _login(client)
    data = _miniprofile_match(client, headers, new)
    note = data["version_note"]
    assert note["has_multiple"] is True
    assert note["count"] == 2
    assert "核对" in note["hint"]
    # 刷新恢复：GET /matches/{id} 同样带提醒
    again = client.get(f"/api/v1/matches/{data['session_id']}",
                       headers=headers).get_json()["data"]
    assert again["version_note"]["count"] == 2


def test_match_has_no_note_for_single_version(app, client):
    only = _doc(app, "校园卡补办流程说明")
    _rule(app, only)
    headers = _login(client)
    data = _miniprofile_match(client, headers, only)
    assert data["version_note"] is None
