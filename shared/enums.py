"""状态枚举与合法转移白名单。

权威来源：docs/20-系统设计/数据模型.md §4、系统设计说明书 §5.3。
枚举一律以字符串字面值存库（不存整型），便于直读数据库。
"""

from __future__ import annotations

# ---------- 角色 ----------
ROLE_STUDENT = "student"
ROLE_ADMIN = "admin"
ROLES = (ROLE_STUDENT, ROLE_ADMIN)

# ---------- 文档解析状态 ----------
PARSE_UPLOADED = "uploaded"
PARSE_PARSING = "parsing"
PARSE_PARSED = "parsed"
PARSE_EMPTY = "empty"
PARSE_FAILED = "failed"
PARSE_CHUNKED = "chunked"
PARSE_INDEXED = "indexed"

PARSE_STATUSES = (
    PARSE_UPLOADED, PARSE_PARSING, PARSE_PARSED,
    PARSE_EMPTY, PARSE_FAILED, PARSE_CHUNKED, PARSE_INDEXED,
)

# 白名单：只有这里列出的 (from, to) 才允许写入
PARSE_TRANSITIONS: dict[str, tuple[str, ...]] = {
    PARSE_UPLOADED: (PARSE_PARSING,),
    PARSE_PARSING: (PARSE_PARSED, PARSE_EMPTY, PARSE_FAILED),
    # parsed/chunked 允许回到 parsing：管理员强制重建解析时必须能从任意
    # 中间态重来，否则只能先"复位到 uploaded"，等于绕过状态机（M3 修正）
    PARSE_PARSED: (PARSE_CHUNKED, PARSE_PARSING),
    PARSE_CHUNKED: (PARSE_INDEXED, PARSE_PARSING),
    PARSE_EMPTY: (PARSE_PARSING,),
    PARSE_FAILED: (PARSE_PARSING,),
    PARSE_INDEXED: (PARSE_PARSING,),
}


def is_parse_transition_allowed(current: str, target: str) -> bool:
    """状态机守卫。非法转移应在服务层抛 409 PARSE_IN_PROGRESS 之外的状态码。"""
    return target in PARSE_TRANSITIONS.get(current, ())


# ---------- 文档时效状态 ----------
EFFECTIVE_ACTIVE = "active"
EFFECTIVE_SUPERSEDED = "superseded"
EFFECTIVE_EXPIRED = "expired"
EFFECTIVE_UNKNOWN = "unknown"

EFFECTIVE_STATUSES = (
    EFFECTIVE_ACTIVE, EFFECTIVE_SUPERSEDED, EFFECTIVE_EXPIRED, EFFECTIVE_UNKNOWN,
)

# ---------- 文件类型 ----------
FILE_TYPE_BY_EXT = {
    "pdf": "pdf",
    "docx": "docx",
    "txt": "txt",
    "md": "md",
}
SUPPORTED_EXTENSIONS = tuple(FILE_TYPE_BY_EXT)

# 魔数：用于识别"改名的可执行文件"（T-01）
MAGIC_PDF = b"%PDF-"
MAGIC_ZIP = b"PK\x03\x04"      # docx 是 zip 容器
