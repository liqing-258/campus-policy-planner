"""健康检查。

对评审的价值：**一条命令看到系统真实能力状态**，而不是只能听作者说。
`llm_mode=degraded` 就是"当前没有可用模型"，前端会据此常驻降级标识，
不允许把降级伪装成 AI 生效（设计说明书 §7.7）。
"""

from __future__ import annotations

from flask import Blueprint

from app.contract import ok
from app.models import Document, DocumentChunk, db
from app.runtime import current_settings

bp = Blueprint("health", __name__)


@bp.get("/health")
def health():
    settings = current_settings()

    db_status = "ok"
    chunk_count = 0
    document_count = 0
    try:
        chunk_count = db.session.query(DocumentChunk).count()
        document_count = db.session.query(Document).filter(
            Document.deleted_at.is_(None)
        ).count()
    except Exception:  # 建表脚本未执行时 /health 应给出可读状态而不是 500
        db.session.rollback()
        db_status = "unavailable"

    return ok({
        "status": "ok" if db_status == "ok" else "degraded",
        "version": settings.version,
        "env": settings.app_env,
        "db": db_status,
        "llm_mode": settings.llm_mode,
        "llm_model": settings.llm_model or None,
        "embedding_mode": settings.embedding_mode,
        "document_count": document_count,
        "chunk_count": chunk_count,
    })
