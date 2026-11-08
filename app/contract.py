"""统一响应外壳与请求追踪。

唯一实现（接口契约统一规范 §2）：任何路由都不得手写 jsonify 字典，
必须走 ok()/fail()，否则外壳字段会漂移。
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from flask import Flask, Response, g, jsonify, request
from werkzeug.exceptions import HTTPException

from app.errors import ApiError, InvalidPagination, InvalidParam
from shared.codes import (
    CODE_MESSAGES,
    FILE_TOO_LARGE,
    INTERNAL_ERROR,
    INVALID_PARAM,
    OK,
)

_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

# 同毫秒内递增，保证 request_id 单调可排序（ULID 规范要求）
_last_ms = 0
_last_rand = 0


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(_ULID_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def new_ulid() -> str:
    """生成 26 位 ULID：48bit 毫秒时间戳 + 80bit 随机数。"""
    global _last_ms, _last_rand
    now_ms = int(time.time() * 1000)
    if now_ms == _last_ms:
        _last_rand += 1
    else:
        _last_ms = now_ms
        _last_rand = int.from_bytes(os.urandom(10), "big")
    timestamp_part = _encode(now_ms, 10)
    random_part = _encode(_last_rand % (1 << 80), 16)
    return timestamp_part + random_part


def utc_now_iso() -> str:
    """契约要求：ISO 8601 UTC，带 Z。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _request_id() -> str:
    rid = getattr(g, "request_id", None)
    if rid is None:
        rid = new_ulid()
        g.request_id = rid
    return rid


def ok(data=None, message: str = "success", code: int = OK,
       http_status: int = 200) -> tuple[Response, int]:
    body = {
        "code": code,
        "message": message,
        "data": data,
        "request_id": _request_id(),
        "timestamp": utc_now_iso(),
    }
    return jsonify(body), http_status


def fail(code: int, message: str | None = None, http_status: int | None = None) -> tuple[Response, int]:
    from shared.codes import CODE_HTTP

    body = {
        "code": code,
        "message": message or CODE_MESSAGES.get(code, "请求处理失败"),
        "data": None,
        "request_id": _request_id(),
        "timestamp": utc_now_iso(),
    }
    return jsonify(body), http_status or CODE_HTTP.get(code, 400)


# ---------- 分页 ----------
MAX_PAGE_SIZE = 100


def page_args() -> tuple[int, int]:
    """解析并校验 page / page_size，非法即抛 1002。"""
    raw_page = request.args.get("page", "1")
    raw_size = request.args.get("page_size", "20")
    try:
        page = int(raw_page)
        page_size = int(raw_size)
    except (TypeError, ValueError):
        raise InvalidPagination("page 与 page_size 必须为整数")
    if page < 1:
        raise InvalidPagination("page 不能小于 1")
    if page_size < 1 or page_size > MAX_PAGE_SIZE:
        raise InvalidPagination(f"page_size 必须在 1–{MAX_PAGE_SIZE} 之间")
    return page, page_size


def body_json() -> dict:
    """读取 JSON 请求体，非法 JSON 抛 1001 而非 500。"""
    payload = request.get_json(silent=True)
    if payload is None:
        if request.data:
            raise InvalidParam("请求体不是合法的 JSON")
        return {}
    if not isinstance(payload, dict):
        raise InvalidParam("请求体必须是 JSON 对象")
    return payload


def paginated(items: list, total: int, page: int, page_size: int) -> dict:
    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "has_more": page * page_size < total,
    }


# ---------- 注册 ----------
def init_contract(app: Flask) -> None:
    @app.before_request
    def _assign_request_id() -> None:
        # 允许网关透传，便于跨服务串联；否则本地生成
        g.request_id = request.headers.get("X-Request-Id") or new_ulid()

    @app.after_request
    def _echo_request_id(response: Response) -> Response:
        response.headers["X-Request-Id"] = _request_id()
        return response

    @app.errorhandler(ApiError)
    def _handle_api_error(exc: ApiError):
        return fail(exc.code, exc.message, exc.status)

    @app.errorhandler(HTTPException)
    def _handle_http_exception(exc: HTTPException):
        # Flask 层的 413 走这里：请求体在进入视图前就被拒了
        if exc.code == 413:
            return fail(FILE_TOO_LARGE, "文件超过大小限制", 413)
        if exc.code == 404:
            # 未匹配到路由：沿用 2002 的语义会让客户端误以为"文档不存在"
            from shared.codes import DOCUMENT_NOT_FOUND

            return fail(DOCUMENT_NOT_FOUND, "接口不存在", 404)
        if exc.code == 405:
            return fail(INVALID_PARAM, "请求方法不允许", 405)
        return fail(INVALID_PARAM, exc.description or "请求非法", exc.code or 400)

    @app.errorhandler(Exception)
    def _handle_unexpected(exc: Exception):
        # 堆栈只进日志，不进响应（安全红线：不泄露内部结构）
        app.logger.exception("unhandled error on %s %s", request.method, request.path)
        return fail(INTERNAL_ERROR, "服务器内部错误，请联系管理员", 500)
