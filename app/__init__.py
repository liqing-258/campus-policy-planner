"""应用工厂。

分域 Blueprint，全部挂在 /api/v1 下。蓝图注册顺序不影响路由查找，
但会影响 url_map 的导出顺序，因此这里按"公开 → 需鉴权"排列，便于人工核对。
"""

from __future__ import annotations

import logging

from flask import Flask, jsonify

from app.config import Settings
from app.contract import init_contract
from app.models import db

API_PREFIX = "/api/v1"


def create_app(overrides: dict | None = None) -> Flask:
    settings = Settings(overrides)

    app = Flask(__name__, static_folder=None, static_url_path="")
    app.config.update(
        SECRET_KEY=settings.jwt_secret,
        # 留 1MB 编码余量：multipart 边界与表单字段会占用少量额外字节，
        # 精确的 20MB 校验由上传视图按 file_size 执行（见 app/documents.py）
        MAX_CONTENT_LENGTH=settings.max_upload_bytes + 1024 * 1024,
        SQLALCHEMY_DATABASE_URI=settings.database_url,
        SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True},
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        PROPAGATE_EXCEPTIONS=False,
    )
    # 中文不转义，键序保持插入顺序（外壳字段按契约固定顺序输出）
    app.json.ensure_ascii = False
    app.json.sort_keys = False

    app.extensions["settings"] = settings
    # 每个 app 实例独立一份 BM25 索引（测试隔离；生产单进程共用一个）
    from app.indexing import EXTENSION_KEY, Bm25Index

    app.extensions[EXTENSION_KEY] = Bm25Index()

    _configure_logging(app, settings)
    settings.ensure_dirs()

    db.init_app(app)
    init_contract(app)

    # ---- 蓝图 ----
    from app import auth as auth_mod
    from app import documents as documents_mod
    from app import health as health_mod
    from app import qa as qa_mod

    app.register_blueprint(health_mod.bp, url_prefix=API_PREFIX)
    app.register_blueprint(auth_mod.bp, url_prefix=API_PREFIX)
    app.register_blueprint(documents_mod.bp, url_prefix=API_PREFIX)
    app.register_blueprint(qa_mod.bp, url_prefix=API_PREFIX)

    _register_root_route(app)

    for warning in settings.secret_warnings:
        app.logger.warning(warning)

    return app


def _register_root_route(app: Flask) -> None:
    """根路径给一句人类可读的说明，避免评审打开 5000 端口看到裸 404。"""

    @app.get("/")
    def root():
        return jsonify({
            "name": "知校 · 校园政策智能理解与事务规划系统",
            "api": API_PREFIX,
            "health": f"{API_PREFIX}/health",
            "docs": "https://github.com/liqing-258/campus-policy-planner",
        })


def _configure_logging(app: Flask, settings: Settings) -> None:
    level = getattr(logging, settings.log_level, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    app.logger.setLevel(level)


def dump_routes(app: Flask) -> list[dict]:
    """导出真实路由表，供文档数字复核（禁止手填接口数）。"""
    rows = []
    for rule in app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        methods = sorted(rule.methods - {"HEAD", "OPTIONS"})
        for method in methods:
            rows.append({
                "method": method,
                "rule": str(rule),
                "endpoint": rule.endpoint,
            })
    return sorted(rows, key=lambda r: (r["rule"], r["method"]))
