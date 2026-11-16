"""文档入库：上传 → 校验 → 落盘 → 解析 → 切分 → 索引。

对应里程碑 M3，测试用例 T-01 ~ T-07。

一个必须说明的实现取舍
---------------------
接口清单 §2.3 原本写的是"上传后异步解析，前端轮询"。实际实现为**同步解析**：

- 依据：单份 20 页 PDF 解析 + 切分 + 建索引实测 < 1s，异步只会引入
  "线程里抛异常无人接""测试无法确定何时完成"两类新问题；
- 代价：超大 PDF（数百页）会让请求变慢，已写入 `README` 已知限制；
- 兼容：响应里仍返回 `parse_status`，前端轮询逻辑无需改动；
  将来要异步化，只需把 `_parse_and_index` 换成入队调用，接口不变。

这条偏差已同步到 `docs/30-接口契约/接口清单.md`，避免文档与代码两套说法。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from flask import Blueprint, current_app, request
from sqlalchemy import func

from app.auth import admin_required, current_user, login_required
from app.chunking import chunk_blocks
from app.contract import new_ulid, ok, page_args, paginated
from app.errors import (
    DocumentDuplicated,
    DocumentNotFound,
    FileTooLarge,
    FileTypeUnsupported,
    InvalidParam,
    ParseInProgress,
)
from app.indexing import rebuild_index
from app.models import AuditLog, Document, DocumentChunk, db
from app.parsing import parse_document
from app.runtime import current_settings
from app.schemas import effective_status, optional_date, optional_text
from app import versions
from shared.enums import (
    EFFECTIVE_ACTIVE,
    EFFECTIVE_STATUSES,
    EFFECTIVE_SUPERSEDED,
    FILE_TYPE_BY_EXT,
    MAGIC_PDF,
    MAGIC_ZIP,
    PARSE_CHUNKED,
    PARSE_EMPTY,
    PARSE_FAILED,
    PARSE_INDEXED,
    PARSE_PARSED,
    PARSE_PARSING,
    PARSE_STATUSES,
    PARSE_UPLOADED,
    SUPPORTED_EXTENSIONS,
    is_parse_transition_allowed,
)

bp = Blueprint("documents", __name__)


# ---------------------------------------------------------------- 工具
def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _serialize(doc: Document, summary: dict | None = None) -> dict:
    """文档出参。`summary` 是规则准备度（R20），由调用方批量算好传进来。

    为什么不在这里查：列表要出 10–50 条，逐条查规则就是 N+1；
    `rule_states.summaries_for()` 一次聚合查询就能给出整页的结果。
    """
    payload = {
        "id": doc.id,
        "title": doc.title,
        "file_name": doc.file_name,
        "file_type": doc.file_type,
        "file_size": doc.file_size,
        "department": doc.department,
        "published_at": doc.published_at.isoformat() if doc.published_at else None,
        "academic_year": doc.academic_year,
        "version_label": doc.version_label,
        "effective_status": doc.effective_status,
        "supersedes_id": doc.supersedes_id,
        "scope_note": doc.scope_note,
        "parse_status": doc.parse_status,
        "chunk_count": doc.chunk_count,
        "created_at": _iso(doc.created_at),
        "updated_at": _iso(doc.updated_at),
        # 资格匹配准备度：五态 + 计数 + 上次抽取结果（详见 app/rule_states.py）
        "rule_summary": summary,
    }
    # 失败给 parse_error，空文本给 parse_hint —— 同一列，按状态选语义化字段名
    if doc.parse_status == PARSE_FAILED:
        payload["parse_error"] = doc.parse_error
        payload["parse_hint"] = None
    elif doc.parse_status == PARSE_EMPTY:
        payload["parse_error"] = None
        payload["parse_hint"] = doc.parse_error
    else:
        payload["parse_error"] = None
        payload["parse_hint"] = None
    return payload


def _audit(action: str, resource_type: str, resource_id: str | None,
           detail: str | None = None) -> None:
    """审计只追加，不记录密码/令牌/完整个人信息。"""
    try:
        db.session.add(AuditLog(
            id=new_ulid(),
            user_id=current_user().id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            detail_json=detail,
            request_id=request.headers.get("X-Request-Id"),
        ))
    except Exception:  # 审计失败不应让主流程 500
        current_app.logger.warning("audit log write failed: %s", action)


def _set_status(doc: Document, target: str) -> None:
    if not is_parse_transition_allowed(doc.parse_status, target):
        raise ParseInProgress(
            f"文档当前状态为 {doc.parse_status}，不允许直接变为 {target}"
        )
    doc.parse_status = target


def _detect_type(filename: str) -> tuple[str, str]:
    """返回 (扩展名, 文件类型)。只认白名单扩展名。"""
    if not filename or "." not in filename:
        raise FileTypeUnsupported("文件名缺少扩展名，仅支持 " + " / ".join(SUPPORTED_EXTENSIONS))
    ext = filename.rsplit(".", 1)[-1].lower()
    if ext not in FILE_TYPE_BY_EXT:
        raise FileTypeUnsupported(
            f"不支持的文件类型 .{ext}，仅支持 " + " / ".join(SUPPORTED_EXTENSIONS)
        )
    return ext, FILE_TYPE_BY_EXT[ext]


def _check_magic(content: bytes, file_type: str, filename: str) -> None:
    """魔数校验：拦住"改了扩展名的可执行文件"（T-01）。

    只看扩展名是不够的 —— 一个 .exe 改名成 .pdf 照样能上传成功，
    然后被解析器当成损坏文件，用户看到的是"解析失败"，而不是"你传错了文件"。
    """
    head = content[:8]
    if file_type == "pdf":
        if not content.startswith(MAGIC_PDF):
            raise FileTypeUnsupported(
                "该文件内容不是 PDF（缺少 %PDF- 文件头）。"
                "请确认没有把其他类型的文件改成 .pdf 扩展名。"
            )
    elif file_type == "docx":
        if not content.startswith(MAGIC_ZIP):
            raise FileTypeUnsupported(
                "该文件内容不是 DOCX（应为 ZIP 容器）。"
                "若是 .doc 老格式，请先用 Word 另存为 .docx。"
            )
    else:
        # txt / md：允许任意文本，但拒绝明显二进制（含 NUL 字节）
        if b"\x00" in content[:4096]:
            raise FileTypeUnsupported("该文件内容为二进制，不是纯文本。")


def _parse_and_index(doc: Document, settings) -> None:
    """解析 → 切分 → 索引。状态机白名单由 _set_status 强制。"""
    stored_path = settings.upload_dir / doc.stored_name

    _set_status(doc, PARSE_PARSING)
    db.session.flush()

    outcome = parse_document(
        stored_path,
        doc.file_type,
        min_text_chars=settings.min_text_chars,
        scanned_page_char_threshold=settings.scanned_page_char_threshold,
    )

    if outcome.status == PARSE_FAILED:
        doc.parse_status = PARSE_FAILED
        doc.parse_error = outcome.error
        doc.chunk_count = 0
        db.session.commit()
        return

    if outcome.status == PARSE_EMPTY:
        doc.parse_status = PARSE_EMPTY
        doc.parse_error = outcome.hint or outcome.error
        doc.chunk_count = 0
        db.session.commit()
        return

    _set_status(doc, PARSE_PARSED)
    chunks = chunk_blocks(
        outcome.blocks,
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
    )

    # 重建片段：先清旧片段（reparse 场景），再整批插入
    db.session.execute(
        db.delete(DocumentChunk).where(DocumentChunk.document_id == doc.id)
    )
    for item in chunks:
        db.session.add(DocumentChunk(
            id=new_ulid(),
            document_id=doc.id,
            chunk_index=item.chunk_index,
            content=item.content,
            char_start=item.char_start,
            char_end=item.char_end,
            page_number=item.page_number,
            section_title=item.section_title,
            token_count=item.token_count,
        ))
    doc.chunk_count = len(chunks)
    doc.parse_status = PARSE_CHUNKED
    doc.parse_error = None
    db.session.commit()

    _set_status(doc, PARSE_INDEXED)
    rebuild_index()
    db.session.commit()


# ---------------------------------------------------------------- 路由
@bp.post("/documents")
@admin_required
def upload():
    settings = current_settings()

    upload_file = request.files.get("file")
    if upload_file is None or upload_file.filename is None:
        raise InvalidParam("缺少文件：请用 multipart/form-data 提交 file 字段")

    filename = upload_file.filename.strip()
    ext, file_type = _detect_type(filename)

    content = upload_file.read()
    size = len(content)
    if size == 0:
        raise InvalidParam("文件内容为空，请检查后重新上传")
    if size > settings.max_upload_bytes:
        raise FileTooLarge(
            f"文件 {size / 1024 / 1024:.1f}MB 超过上限 {settings.max_upload_mb}MB"
        )
    _check_magic(content, file_type, filename)

    file_hash = hashlib.sha256(content).hexdigest()
    duplicate = db.session.execute(
        db.select(Document).where(
            Document.file_hash == file_hash,
            Document.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        raise DocumentDuplicated(
            f"该文件已存在于知识库（{duplicate.title}），无需重复上传"
        )

    title = optional_text(request.form.get("title"), "title") or filename.rsplit(".", 1)[0]
    doc = Document(
        id=new_ulid(),
        title=title[:200],
        file_name=filename[:255],
        stored_name=f"{new_ulid()}.{ext}",
        file_type=file_type,
        file_size=size,
        file_hash=file_hash,
        department=optional_text(request.form.get("department"), "department"),
        published_at=optional_date(request.form.get("published_at")),
        academic_year=optional_text(request.form.get("academic_year"), "academic_year"),
        version_label=optional_text(request.form.get("version_label"), "version_label"),
        effective_status=effective_status(request.form.get("effective_status")),
        scope_note=optional_text(request.form.get("scope_note"), "scope_note"),
        parse_status=PARSE_UPLOADED,
        uploader_id=current_user().id,
    )

    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    (settings.upload_dir / doc.stored_name).write_bytes(content)

    db.session.add(doc)
    db.session.commit()
    _audit("document.upload", "document", doc.id, f'{{"file_name":"{filename}"}}')
    db.session.commit()

    try:
        _parse_and_index(doc, settings)
    except ParseInProgress:
        raise
    except Exception as exc:  # 解析层未预料的异常：标记失败，不返回 500
        current_app.logger.exception("parse failed for %s", doc.id)
        doc.parse_status = PARSE_FAILED
        doc.parse_error = f"解析时出现未预期错误：{type(exc).__name__}"
        db.session.commit()

    from app.rule_states import summary_of
    payload = _serialize(doc, summary_of(doc.id))
    # R15：上传完成即提示"是否属于政策更新"。只提示、不自动建联——
    # 未经管理员确认的文件不会被标成现行有效，也不会顶替旧版。
    payload["possible_updates"] = versions.candidates_for(doc.title, exclude_id=doc.id)
    return ok(payload, http_status=201)


@bp.get("/documents")
@login_required
def list_documents():
    page, page_size = page_args()

    stmt = db.select(Document).where(Document.deleted_at.is_(None))
    parse_status = request.args.get("parse_status")
    if parse_status:
        if parse_status not in PARSE_STATUSES:
            raise InvalidParam(f"parse_status 必须是 {'/'.join(PARSE_STATUSES)} 之一")
        stmt = stmt.where(Document.parse_status == parse_status)

    eff = request.args.get("effective_status")
    if eff:
        if eff not in EFFECTIVE_STATUSES:
            raise InvalidParam(f"effective_status 必须是 {'/'.join(EFFECTIVE_STATUSES)} 之一")
        stmt = stmt.where(Document.effective_status == eff)

    keyword = (request.args.get("q") or "").strip()
    if keyword:
        stmt = stmt.where(Document.title.contains(keyword))

    total = db.session.execute(
        db.select(func.count()).select_from(stmt.subquery())
    ).scalar_one()
    rows = db.session.execute(
        stmt.order_by(Document.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).scalars().all()

    # 整页一次算出规则准备度，避免逐条查规则（N+1）
    from app.rule_states import summaries_for
    summaries = summaries_for([d.id for d in rows])

    return ok(paginated(
        [_serialize(d, summaries.get(d.id)) for d in rows], total, page, page_size
    ))


@bp.get("/documents/<document_id>")
@login_required
def get_document(document_id: str):
    doc = _get_active_or_404(document_id)
    from app.rule_states import summary_of
    return ok(_serialize(doc, summary_of(doc.id)))


@bp.patch("/documents/<document_id>")
@admin_required
def update_document(document_id: str):
    """修改文档的版本元数据（M6）。

    **为什么单开一个接口**

    `published_at` / `academic_year` / `effective_status` 三列 M3 建表时就有，但只在
    **上传那一刻**能写。上传时忘了标，之后没有任何入口能补——而删掉重传会换
    `document_id`，挂在旧 id 上的已复核规则、历史会话引用全部作废。版本冲突检测
    （T-12）恰恰依赖这三项，所以补一个"只改元数据、不碰文件与片段"的接口。

    **为什么只更新请求里出现的键**

    前端"只改有效状态"时不会带 `title`。若把缺失键当成空值写回，标题会被清空，
    而列表页的标题同时是检索召回后的展示名——这种覆盖式更新很难被发现。
    """
    doc = _get_active_or_404(document_id)
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        raise InvalidParam("请求体必须是 JSON 对象")

    if "title" in data:
        title = optional_text(data.get("title"), "title")
        if not title:
            raise InvalidParam("title 不能为空")
        doc.title = title
    for field in ("department", "academic_year", "version_label", "scope_note"):
        if field in data:
            setattr(doc, field, optional_text(data.get(field), field))
    if "published_at" in data:
        doc.published_at = optional_date(data.get("published_at"))
    if "effective_status" in data:
        # 清空等于"未标注"，不是"沿用旧值"：把状态改回 unknown 是正当操作
        doc.effective_status = effective_status(data.get("effective_status"))

    doc.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    # 只记"改了哪几个字段"，不记值：元数据里可能带部门等内部信息，
    # 审计日志要的是可追溯，不是副本。
    _audit("document.update", "document", doc.id, json.dumps({"fields": sorted(data)}))
    db.session.commit()
    from app.rule_states import summary_of
    return ok(_serialize(doc, summary_of(doc.id)))


@bp.get("/documents/version-candidates")
@admin_required
def version_candidates():
    """上传预检：与给定标题同主题或高度相似的既有文件（R15）。

    存在同主题文件**不是错误**——政策本来就该有新旧版本，所以不拦上传，
    只回答"像不像政策更新"。判断与建联是管理员的事，这里只给候选。
    """
    title = optional_text(request.args.get("title"), "title")
    if not title:
        raise InvalidParam("缺少 title 参数")
    return ok(versions.candidates_for(title))


@bp.get("/documents/<document_id>/versions")
@login_required
def document_versions(document_id: str):
    """同一政策的全部版本（主题组 ∪ 人工 supersedes 链），新在前（R15）。

    学生与管理员都能看：历史版本公开是溯源的一部分。
    """
    doc = _get_active_or_404(document_id)
    family = versions.related_versions(doc)
    # "最新版本"优先取人工链上替代了这一版的那份（见 versions.replacer_of）
    newest = versions.replacer_of(doc, family) or (family[0] if family else doc)
    active = [d for d in family if d.effective_status == EFFECTIVE_ACTIVE]
    if len(active) == 1:
        hint = f"当前标注「现行有效」的是《{active[0].title}》。"
    elif not active:
        hint = "没有任何一版被标注「现行有效」，请以最新版原文为准并咨询发布部门。"
    else:
        hint = "多份文件都被标注「现行有效」，标注本身存在冲突，请人工核对。"
    return ok({
        "document_id": doc.id,
        "newest_document_id": newest.id,
        "count": len(family),
        "versions": [
            dict(versions.brief(d), is_current=(d.id == doc.id))
            for d in family
        ],
        "hint": hint,
    })


@bp.post("/documents/<document_id>/version-link")
@admin_required
def version_link(document_id: str):
    """管理员确认版本关系（R15）：一次做三件事。

    1. 新版记 `supersedes_id`（谁替代了谁）；
    2. 旧版标「已被替代」；
    3. 新版标"相应有效状态"（默认「现行有效」，可显式传其他值）。

    **这是全仓唯一能把文件标成「现行有效」的自动动作**，而它只由管理员
    显式调用触发——"不得自动将未经管理员确认的文件标记为现行有效"因此成立：
    上传不改状态、预检不改状态、只有这一条接口改，且带审计。
    """
    doc = _get_active_or_404(document_id)
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        raise InvalidParam("请求体必须是 JSON 对象")

    old_id = optional_text(data.get("supersedes_id"), "supersedes_id")
    if not old_id:
        raise InvalidParam("supersedes_id 不能为空")
    if old_id == doc.id:
        raise InvalidParam("不能把文件关联为自己的历史版本")
    old = db.session.get(Document, old_id)
    if old is None or old.deleted_at is not None:
        raise DocumentNotFound("被替代的旧版本不存在")

    # 防环：沿旧版的 supersedes 链走，能走回 doc 就说明会成环
    cursor, guard = old, 0
    while cursor is not None and guard < 20:
        if cursor.id == doc.id:
            raise InvalidParam("该关联会形成版本环：新版本已是旧版本的历史版本")
        nxt = cursor.supersedes_id
        cursor = db.session.get(Document, nxt) if nxt else None
        guard += 1

    new_status = effective_status(data.get("new_status", EFFECTIVE_ACTIVE))
    doc.supersedes_id = old.id
    old.effective_status = EFFECTIVE_SUPERSEDED
    doc.effective_status = new_status
    doc.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    old.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    _audit("document.version-link", "document", doc.id,
           json.dumps({"supersedes_id": old.id, "new_status": new_status}))
    db.session.commit()
    from app.rule_states import summaries_for
    pair = summaries_for([doc.id, old.id])
    return ok({
        "document": _serialize(doc, pair.get(doc.id)),
        "superseded": _serialize(old, pair.get(old.id)),
    })


@bp.delete("/documents/<document_id>")
@admin_required
def delete_document(document_id: str):
    """软删除：保留记录与片段，检索时排除，历史引用不会断（US-10）。"""
    doc = _get_active_or_404(document_id)
    doc.deleted_at = datetime.now(timezone.utc).replace(tzinfo=None)
    _audit("document.delete", "document", doc.id)
    db.session.commit()
    rebuild_index()
    return ok({"id": doc.id, "deleted": True})


@bp.post("/documents/<document_id>/reparse")
@admin_required
def reparse_document(document_id: str):
    settings = current_settings()
    doc = _get_active_or_404(document_id)
    if doc.parse_status == PARSE_PARSING:
        raise ParseInProgress("该文档正在解析中，请稍后再试")

    stored_path = settings.upload_dir / doc.stored_name
    if not stored_path.exists():
        raise DocumentNotFound("原始文件已不存在，无法重新解析；请重新上传")

    # 状态机已允许 parsed/chunked/indexed/empty/failed → parsing，
    # 因此这里不需要"先复位到 uploaded"的绕过写法
    if not is_parse_transition_allowed(doc.parse_status, PARSE_PARSING):
        raise ParseInProgress(
            f"文档当前状态为 {doc.parse_status}，无法重新解析"
        )
    _audit("document.reparse", "document", doc.id)
    db.session.commit()

    _parse_and_index(doc, settings)
    from app.rule_states import summary_of
    return ok(_serialize(doc, summary_of(doc.id)))


@bp.get("/documents/<document_id>/chunks")
@login_required
def list_chunks(document_id: str):
    page, page_size = page_args()
    doc = _get_active_or_404(document_id)

    total = db.session.execute(
        db.select(func.count(DocumentChunk.id)).where(DocumentChunk.document_id == doc.id)
    ).scalar_one()
    rows = db.session.execute(
        db.select(DocumentChunk)
        .where(DocumentChunk.document_id == doc.id)
        .order_by(DocumentChunk.chunk_index)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).scalars().all()

    items = [_chunk_payload(c, doc.title) for c in rows]
    return ok(paginated(items, total, page, page_size))


@bp.get("/chunks/<chunk_id>")
@login_required
def get_chunk(chunk_id: str):
    chunk = db.session.get(DocumentChunk, chunk_id)
    if chunk is None:
        raise DocumentNotFound("片段不存在")
    doc = _get_active_or_404(chunk.document_id)

    neighbours = db.session.execute(
        db.select(DocumentChunk)
        .where(
            DocumentChunk.document_id == doc.id,
            DocumentChunk.chunk_index.in_(
                [chunk.chunk_index - 1, chunk.chunk_index + 1]
            ),
        )
        .order_by(DocumentChunk.chunk_index)
    ).scalars().all()

    payload = _chunk_payload(chunk, doc.title)
    payload["document"] = {
        "id": doc.id,
        "title": doc.title,
        "file_name": doc.file_name,
        "effective_status": doc.effective_status,
    }
    payload["context"] = [
        {
            "chunk_index": c.chunk_index,
            "page_number": c.page_number,
            "section_title": c.section_title,
            "preview": c.content[:80],
        }
        for c in neighbours
    ]
    return ok(payload)


# ---------------------------------------------------------------- 内部
def _get_active_or_404(document_id: str) -> Document:
    doc = db.session.get(Document, document_id)
    if doc is None or doc.deleted_at is not None:
        raise DocumentNotFound("文档不存在或已被删除")
    return doc


def _chunk_payload(chunk: DocumentChunk, document_title: str) -> dict:
    return {
        "id": chunk.id,
        "document_id": chunk.document_id,
        "document_title": document_title,
        "chunk_index": chunk.chunk_index,
        "content": chunk.content,
        "char_start": chunk.char_start,
        "char_end": chunk.char_end,
        "page_number": chunk.page_number,
        "section_title": chunk.section_title,
        "token_count": chunk.token_count,
    }
