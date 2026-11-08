"""请求契约（pydantic v2）。

为什么单独一个文件：契约规范要求"请求体 extra='forbid'，未知字段直接 1001"。
把校验集中在这里，路由函数只接受已校验的数据，避免各处手写 if 判断。
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.errors import InvalidParam


class StrictModel(BaseModel):
    """统一开启未知字段拒绝。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class LoginRequest(StrictModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=72)


def validate(schema: type[StrictModel], payload: dict) -> BaseModel:
    """统一把 pydantic 的 ValidationError 翻译成 1001，并给出可读字段名。"""
    try:
        return schema.model_validate(payload)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(x) for x in first.get("loc", ())) or "请求体"
        reason = first.get("msg", "格式不正确")
        if first.get("type") == "extra_forbidden":
            reason = "不允许的字段"
        raise InvalidParam(f"参数校验失败：{loc} {reason}")


# ---------- 文档元数据字段 ----------
EFFECTIVE_STATUS_VALUES = ("active", "superseded", "expired", "unknown")
DOCUMENT_TEXT_FIELDS = {
    "title": 200,
    "department": 100,
    "academic_year": 16,
    "version_label": 32,
    "scope_note": 200,
}


def optional_text(raw: str | None, field: str) -> str | None:
    """上传表单里的可选文本字段：空串归一为 None，超长直接 1001。"""
    if raw is None:
        return None
    value = raw.strip()
    if value == "":
        return None
    limit = DOCUMENT_TEXT_FIELDS.get(field)
    if limit and len(value) > limit:
        raise InvalidParam(f"{field} 长度不能超过 {limit} 个字符")
    return value


def effective_status(raw: str | None) -> str:
    if raw is None or raw.strip() == "":
        return "unknown"
    value = raw.strip()
    if value not in EFFECTIVE_STATUS_VALUES:
        raise InvalidParam(
            "effective_status 必须是 " + "/".join(EFFECTIVE_STATUS_VALUES) + " 之一"
        )
    return value


def optional_date(raw: str | None, field: str = "published_at") -> date | None:
    if raw is None or raw.strip() == "":
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        raise InvalidParam(f"{field} 必须是 YYYY-MM-DD 格式")
