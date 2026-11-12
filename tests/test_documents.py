"""M3 验收：文档入库（T-01 ~ T-07）+ 权限与状态机。

每个用例对应 `系统设计说明书` §11.2 的一条编号，文件名与用例号一一对照，
方便评审把"测试通过"直接映射到"门禁通过"。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

DEMO = Path(__file__).resolve().parent.parent / "demo"
# 知识库演示文档（会被自举导入，用户看得见）
PDF_OK = DEMO / "01-本科生奖学金评审办法（2026年版）.pdf"
DOCX_OK = DEMO / "02-学生事务办理指南.docx"
# 异常样本只给自动化测试用，放在 fixtures/ 下，不进知识库
PDF_SCANNED = DEMO / "fixtures" / "扫描件样例.pdf"
TXT_EMPTY = DEMO / "fixtures" / "空文本样例.txt"
PDF_FAKE = DEMO / "fixtures" / "改名的假文件.pdf"


def upload(client, headers, path: Path, **form):
    with open(path, "rb") as handle:
        data = {"file": (handle, path.name)}
        data.update(form)
        return client.post("/api/v1/documents", headers=headers, data=data,
                           content_type="multipart/form-data")


@pytest.fixture(scope="module", autouse=True)
def _demo_files_exist():
    missing = [p.name for p in (PDF_OK, DOCX_OK, PDF_SCANNED, TXT_EMPTY, PDF_FAKE)
               if not p.exists()]
    assert not missing, f"演示文件缺失：{missing}，请先运行 tools/make_demo_docs.py"


# ---------------------------------------------------------------- T-01
def test_t01_renamed_executable_is_rejected(client, admin_headers):
    resp = upload(client, admin_headers, PDF_FAKE)
    assert resp.status_code == 415
    body = resp.get_json()
    assert body["code"] == 1005
    assert "%PDF-" in body["message"]


def test_unsupported_extension_is_rejected(client, admin_headers, tmp_path):
    bad = tmp_path / "policy.exe"
    bad.write_bytes(b"MZ\x90\x00")
    resp = upload(client, admin_headers, bad)
    assert resp.status_code == 415
    assert resp.get_json()["code"] == 1005


# ---------------------------------------------------------------- T-02
def test_t02_oversized_file_is_rejected(client, admin_headers, tmp_path):
    big = tmp_path / "huge.pdf"
    big.write_bytes(b"%PDF-1.7\n" + b"0" * (30 * 1024 * 1024))
    resp = upload(client, admin_headers, big)
    assert resp.status_code == 413
    assert resp.get_json()["code"] == 1004


# ---------------------------------------------------------------- T-03
def test_t03_encrypted_pdf_fails_with_readable_reason(client, admin_headers, tmp_path):
    import pymupdf

    source = pymupdf.open()
    page = source.new_page()
    page.insert_text((72, 100), "This document is encrypted.", fontsize=12)
    target = tmp_path / "encrypted.pdf"
    source.save(str(target), encryption=pymupdf.PDF_ENCRYPT_AES_256,
                owner_pw="owner-secret", user_pw="user-secret")
    source.close()

    resp = upload(client, admin_headers, target)
    assert resp.status_code == 201
    data = resp.get_json()["data"]
    assert data["parse_status"] == "failed"
    assert "加密" in data["parse_error"]
    assert data["chunk_count"] == 0


# ---------------------------------------------------------------- T-04
def test_t04_scanned_pdf_reports_empty_with_hint(client, admin_headers):
    resp = upload(client, admin_headers, PDF_SCANNED)
    assert resp.status_code == 201
    data = resp.get_json()["data"]
    assert data["parse_status"] == "empty"
    assert "扫描件" in data["parse_hint"]
    assert "OCR" in data["parse_hint"]
    assert data["chunk_count"] == 0


# ---------------------------------------------------------------- T-05
def test_t05_empty_txt_produces_no_chunks(client, admin_headers):
    resp = upload(client, admin_headers, TXT_EMPTY)
    data = resp.get_json()["data"]
    assert data["parse_status"] == "empty"
    assert data["chunk_count"] == 0

    chunks = client.get(f"/api/v1/documents/{data['id']}/chunks",
                        headers=admin_headers).get_json()["data"]
    assert chunks["total"] == 0
    assert chunks["items"] == []


# ---------------------------------------------------------------- T-06
def test_t06_normal_pdf_is_indexed_quickly_with_correct_pages(client, admin_headers):
    started = time.perf_counter()
    resp = upload(client, admin_headers, PDF_OK,
                  title="（演示样例）本科生奖学金评审办法",
                  department="学生资助管理中心",
                  academic_year="2026-2027",
                  effective_status="active")
    elapsed = time.perf_counter() - started

    assert resp.status_code == 201
    data = resp.get_json()["data"]
    assert data["parse_status"] == "indexed"
    assert data["chunk_count"] >= 3
    assert elapsed < 15, f"解析耗时 {elapsed:.2f}s，超过 T-06 的 15s 上限"

    chunks = client.get(f"/api/v1/documents/{data['id']}/chunks?page_size=100",
                        headers=admin_headers).get_json()["data"]
    pages = [c["page_number"] for c in chunks["items"]]
    assert pages == sorted(pages), "片段页码没有按顺序排列"
    assert set(pages) == {1, 2, 3, 4}, f"页码应覆盖 1–4 页，实际 {sorted(set(pages))}"
    # 每条片段都必须能定位：有页码且有章节
    for item in chunks["items"]:
        assert item["page_number"] is not None
        assert item["section_title"], "片段缺少章节标题，无法回溯原文位置"
        assert item["char_start"] < item["char_end"]


# ---------------------------------------------------------------- T-07
def test_t07_normal_docx_keeps_order_and_headings(client, admin_headers):
    resp = upload(client, admin_headers, DOCX_OK, title="（演示样例）学生事务办理指南")
    data = resp.get_json()["data"]
    assert data["parse_status"] == "indexed"
    assert data["chunk_count"] >= 4

    chunks = client.get(f"/api/v1/documents/{data['id']}/chunks?page_size=100",
                        headers=admin_headers).get_json()["data"]["items"]
    titles = [c["section_title"] for c in chunks]
    assert "一、事项范围" in titles
    assert "二、办理流程" in titles
    # 顺序必须与原文一致（DOCX 无页码，靠 chunk_index 保证）
    assert [c["chunk_index"] for c in chunks] == sorted(c["chunk_index"] for c in chunks)
    assert titles.index("一、事项范围") < titles.index("二、办理流程")

    # 表格内容不能被丢掉
    joined = "".join(c["content"] for c in chunks)
    assert "在读证明" in joined
    assert "2 个工作日" in joined


# ---------------------------------------------------------------- 权限
def test_t22_student_cannot_upload(client, student_headers):
    resp = upload(client, student_headers, PDF_OK)
    assert resp.status_code == 403
    assert resp.get_json()["code"] == 3002


def test_student_can_read_document_list(client, admin_headers, student_headers):
    upload(client, admin_headers, PDF_OK, title="（演示样例）学生可读的文档")
    resp = client.get("/api/v1/documents", headers=student_headers)
    assert resp.status_code == 200
    assert resp.get_json()["data"]["total"] >= 1


# ---------------------------------------------------------------- 业务规则
def test_duplicate_upload_is_rejected(client, admin_headers):
    upload(client, admin_headers, PDF_OK, title="（演示样例）第一次上传")
    resp = upload(client, admin_headers, PDF_OK, title="（演示样例）第二次上传")
    assert resp.status_code == 409
    assert resp.get_json()["code"] == 2001


def test_missing_file_field_is_1001(client, admin_headers):
    resp = client.post("/api/v1/documents", headers=admin_headers, data={},
                       content_type="multipart/form-data")
    assert resp.get_json()["code"] == 1001


def test_invalid_effective_status_is_1001(client, admin_headers):
    resp = upload(client, admin_headers, PDF_OK, effective_status="bogus")
    assert resp.get_json()["code"] == 1001


def test_soft_delete_hides_document_but_keeps_row(client, admin_headers, app):
    created = upload(client, admin_headers, DOCX_OK, title="（演示样例）待删除文档"
                     ).get_json()["data"]
    doc_id = created["id"]

    resp = client.delete(f"/api/v1/documents/{doc_id}", headers=admin_headers)
    assert resp.status_code == 200

    assert client.get(f"/api/v1/documents/{doc_id}",
                      headers=admin_headers).get_json()["code"] == 2002
    listing = client.get("/api/v1/documents?page_size=100",
                         headers=admin_headers).get_json()["data"]
    assert doc_id not in [item["id"] for item in listing["items"]]

    from app.models import Document, db
    with app.app_context():
        row = db.session.get(Document, doc_id)
        assert row is not None, "软删除不应物理删除记录"
        assert row.deleted_at is not None


def test_deleted_document_leaves_index(client, admin_headers, app):
    """删除后不应再被检索到——否则用户会点进一个 404 引用。"""
    from app.indexing import get_index

    created = upload(client, admin_headers, PDF_OK, title="（演示样例）将删除并退出索引"
                     ).get_json()["data"]
    client.delete(f"/api/v1/documents/{created['id']}", headers=admin_headers)

    with app.app_context():
        hits = get_index().search("国家励志奖学金 申请条件", top_k=10)
    assert created["id"] not in [hit.document_id for hit in hits]


def test_reparse_rebuilds_chunks(client, admin_headers):
    created = upload(client, admin_headers, PDF_OK, title="（演示样例）重解析测试"
                     ).get_json()["data"]
    before = created["chunk_count"]

    resp = client.post(f"/api/v1/documents/{created['id']}/reparse", headers=admin_headers)
    assert resp.status_code == 200
    after = resp.get_json()["data"]
    assert after["parse_status"] == "indexed"
    assert after["chunk_count"] == before, "同一文件重复解析的片段数应保持一致"


def test_chunk_detail_returns_context_and_document(client, admin_headers):
    created = upload(client, admin_headers, PDF_OK, title="（演示样例）片段详情测试"
                     ).get_json()["data"]
    chunks = client.get(f"/api/v1/documents/{created['id']}/chunks",
                        headers=admin_headers).get_json()["data"]["items"]
    target = chunks[1]

    payload = client.get(f"/api/v1/chunks/{target['id']}",
                         headers=admin_headers).get_json()["data"]
    assert payload["document"]["id"] == created["id"]
    assert len(payload["context"]) == 2, "中间片段应返回前后各一条上下文"
    assert [c["chunk_index"] for c in payload["context"]] == [0, 2]


def test_list_filters_by_parse_status(client, admin_headers):
    upload(client, admin_headers, PDF_SCANNED, title="（演示样例）扫描件筛选")
    data = client.get("/api/v1/documents?parse_status=empty&page_size=100",
                      headers=admin_headers).get_json()["data"]
    assert data["total"] >= 1
    assert all(item["parse_status"] == "empty" for item in data["items"])


def test_invalid_parse_status_filter_is_1001(client, admin_headers):
    resp = client.get("/api/v1/documents?parse_status=whatever", headers=admin_headers)
    assert resp.get_json()["code"] == 1001


def test_unknown_document_is_2002(client, admin_headers):
    resp = client.get("/api/v1/documents/01NOTEXIST0000000000000000", headers=admin_headers)
    assert resp.status_code == 404
    assert resp.get_json()["code"] == 2002


def test_pagination_out_of_range_is_1002(client, admin_headers):
    resp = client.get("/api/v1/documents?page=0", headers=admin_headers)
    assert resp.get_json()["code"] == 1002


# ---------------------------------------------------------------- M6：版本元数据
def test_patch_updates_version_metadata(client, admin_headers):
    """版本冲突检测依赖这三项，上传后能补是它可用的前提。"""
    created = upload(client, admin_headers, PDF_OK, title="（演示样例）元数据补录"
                     ).get_json()["data"]
    assert created["academic_year"] is None
    assert created["effective_status"] == "unknown"

    resp = client.patch(f"/api/v1/documents/{created['id']}", headers=admin_headers, json={
        "academic_year": "2025-2026",
        "published_at": "2025-09-01",
        "effective_status": "active",
        "version_label": "2025版",
    })
    assert resp.status_code == 200
    body = resp.get_json()["data"]
    assert body["academic_year"] == "2025-2026"
    assert body["published_at"] == "2025-09-01"
    assert body["effective_status"] == "active"
    assert body["version_label"] == "2025版"
    # 只改元数据：解析结果与片段不能受牵连，否则补一次元数据就要重建索引
    assert body["chunk_count"] == created["chunk_count"]
    assert body["parse_status"] == created["parse_status"]


def test_patch_only_touches_given_fields(client, admin_headers):
    """只改「有效状态」时不能把标题顺手清空——覆盖式更新很难被发现。"""
    created = upload(client, admin_headers, PDF_OK, title="（演示样例）只改一个字段"
                     ).get_json()["data"]
    body = client.patch(f"/api/v1/documents/{created['id']}", headers=admin_headers,
                        json={"effective_status": "superseded"}).get_json()["data"]
    assert body["title"] == "（演示样例）只改一个字段"
    assert body["effective_status"] == "superseded"


def test_patch_can_clear_metadata_back_to_unknown(client, admin_headers):
    created = upload(client, admin_headers, PDF_OK, title="（演示样例）撤回标注",
                     published_at="2025-09-01", effective_status="active"
                     ).get_json()["data"]
    body = client.patch(f"/api/v1/documents/{created['id']}", headers=admin_headers,
                        json={"published_at": "", "effective_status": "unknown"}
                        ).get_json()["data"]
    assert body["published_at"] is None
    assert body["effective_status"] == "unknown"


def test_patch_rejects_bad_effective_status(client, admin_headers):
    created = upload(client, admin_headers, PDF_OK, title="（演示样例）非法状态"
                     ).get_json()["data"]
    resp = client.patch(f"/api/v1/documents/{created['id']}", headers=admin_headers,
                        json={"effective_status": "effective"})
    assert resp.status_code == 400
    assert resp.get_json()["code"] == 1001


def test_patch_empty_title_is_rejected(client, admin_headers):
    created = upload(client, admin_headers, PDF_OK, title="（演示样例）空标题"
                     ).get_json()["data"]
    resp = client.patch(f"/api/v1/documents/{created['id']}", headers=admin_headers,
                        json={"title": "   "})
    assert resp.get_json()["code"] == 1001


def test_patch_requires_admin(client, admin_headers, student_headers):
    created = upload(client, admin_headers, PDF_OK, title="（演示样例）越权改元数据"
                     ).get_json()["data"]
    resp = client.patch(f"/api/v1/documents/{created['id']}", headers=student_headers,
                        json={"effective_status": "active"})
    assert resp.status_code == 403
    assert resp.get_json()["code"] == 3002


def test_patch_unknown_document_is_2002(client, admin_headers):
    resp = client.patch("/api/v1/documents/01NOTEXIST0000000000000000",
                        headers=admin_headers, json={"effective_status": "active"})
    assert resp.status_code == 404
    assert resp.get_json()["code"] == 2002
