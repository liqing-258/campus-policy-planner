"""首次运行自举：建表 → 演示账号 → 示例政策。

打包成 exe 之后，用户拿到的只有一个 exe（外加 data/ 与几个 bat）。
双击就要求它"能用"，不能指望用户去跑 `tools/init_db.py`。所以启动时做三件事：

1. 表不存在就建（`create_all` 幂等）
2. 两个演示账号不存在就写（密码是公开的演示口令）
3. 知识库为空、且能找到 demo/ 时，导入示例 PDF（解析 → 切分 → 索引）

第 3 步只在"空库"时执行：老用户升级 exe 不该被塞进重复文件。
所有步骤幂等，重复执行只输出"已就绪"。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from app.paths import demo_dir

DEMO_USERS = (
    ("student", "student123", "演示学生", "student"),
    ("admin", "admin123", "演示管理员", "admin"),
)

# 示例文件名里的元数据：`01-本科生奖学金评审办法（2026年版）.pdf`
_VERSION_RE = re.compile(r"（(\d{4})年版）")
_PREFIX_RE = re.compile(r"^\d+[-_\s]*")


def _title_of(path: Path) -> str:
    return _PREFIX_RE.sub("", path.stem)[:200]


def _year_of(path: Path) -> int | None:
    matched = _VERSION_RE.search(path.stem)
    return int(matched.group(1)) if matched else None


def ensure_schema() -> int:
    """建表。返回新建的表数量（0 表示已存在）。"""
    from sqlalchemy import inspect

    from app.models import db

    existing = set(inspect(db.engine).get_table_names())
    db.create_all()
    now = set(inspect(db.engine).get_table_names())
    return len(now - existing)


def ensure_users() -> int:
    """写入两个演示账号，返回新增数量。"""
    from app.auth import hash_password
    from app.contract import new_ulid
    from app.models import User, db

    created = 0
    for username, password, display_name, role in DEMO_USERS:
        found = db.session.execute(
            db.select(User).where(User.username == username)
        ).scalar_one_or_none()
        if found is not None:
            continue
        db.session.add(User(
            id=new_ulid(),
            username=username,
            password_hash=hash_password(password),
            display_name=display_name,
            role=role,
        ))
        created += 1
    if created:
        db.session.commit()
    return created


def ensure_documents() -> int:
    """知识库为空时导入 demo/ 下的示例政策，返回导入数量。"""
    from app.contract import new_ulid
    from app.documents import _parse_and_index
    from app.models import Document, User, db
    from app.runtime import current_settings

    source = demo_dir()
    if source is None:
        return 0

    active = db.session.execute(
        db.select(Document).where(Document.deleted_at.is_(None))
    ).first()
    if active is not None:
        return 0

    settings = current_settings()
    admin = db.session.execute(
        db.select(User).where(User.role == "admin")
    ).scalar_one_or_none()
    if admin is None:
        return 0

    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(
        p for p in source.iterdir()
        if p.is_file() and p.suffix.lower() in (".pdf", ".docx", ".txt", ".md")
    )

    imported = 0
    for path in files:
        ext = path.suffix.lower().lstrip(".")
        file_type = "pdf" if ext == "pdf" else "docx" if ext == "docx" else "md" if ext == "md" else "txt"
        content = path.read_bytes()
        stored_name = f"{new_ulid()}.{ext}"
        (settings.upload_dir / stored_name).write_bytes(content)

        year = _year_of(path)
        doc = Document(
            id=new_ulid(),
            title=_title_of(path),
            file_name=path.name[:255],
            stored_name=stored_name,
            file_type=file_type,
            file_size=len(content),
            file_hash=hashlib.sha256(content).hexdigest(),
            academic_year=f"{year}-{year + 1}" if year else None,
            effective_status="active",
            parse_status="uploaded",
            uploader_id=admin.id,
        )
        db.session.add(doc)
        db.session.commit()

        try:
            _parse_and_index(doc, settings)
            imported += 1
        except Exception as exc:  # 单个文件失败不该让整个自举失败
            doc.parse_status = "failed"
            doc.parse_error = f"自举导入失败：{type(exc).__name__}"
            db.session.commit()
            print(f"[自举] 跳过 {path.name}：{type(exc).__name__}")

    return imported


def ensure_ready(quiet: bool = False) -> dict[str, int]:
    """一次性把三件事做完，返回各自的新增数量。调用方需已在 app_context 内。"""
    tables = ensure_schema()
    users = ensure_users()
    documents = ensure_documents()
    if not quiet:
        print(f"[自举] 建表 {tables} 张 · 新增账号 {users} 个 · 导入政策 {documents} 份", flush=True)
    return {"tables": tables, "users": users, "documents": documents}
