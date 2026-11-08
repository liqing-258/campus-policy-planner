"""配置集中读取。

约定（与 docs/20-系统设计/系统设计说明书.md §10.4 一致）：
- 所有环境变量只在本文件读取一次，其它模块一律 `from app.config import settings` 或
  从 `current_app.extensions["settings"]` 取，禁止散落 `os.getenv`。
- 相对路径统一以项目根目录为基准解析，避免"换个工作目录就找不到 data/"。
- 任何密钥都不写默认值，只在缺失时给出明确告警或降级。
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PLACEHOLDER_SECRET = "please-replace-with-a-random-32-byte-hex"


def _str(key: str, default: str = "") -> str:
    value = os.getenv(key)
    return default if value is None or value.strip() == "" else value.strip()


def _int(key: str, default: int) -> int:
    raw = _str(key, "")
    if raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float(key: str, default: float) -> float:
    raw = _str(key, "")
    if raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _bool(key: str, default: bool) -> bool:
    raw = _str(key, "").lower()
    if raw == "":
        return default
    return raw in ("1", "true", "yes", "on")


def _abs_path(raw: str) -> Path:
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


class Settings:
    """一次实例化的运行时配置。测试可用 overrides 覆盖任意字段。"""

    def __init__(self, overrides: dict | None = None) -> None:
        # 只在首次构造时加载 .env；已存在的进程环境变量优先级更高（load_dotenv 默认不覆盖）
        load_dotenv(PROJECT_ROOT / ".env", override=False)

        self.app_env = _str("APP_ENV", "dev")
        self.log_level = _str("LOG_LEVEL", "INFO").upper()
        self.host = _str("HOST", "127.0.0.1")
        self.port = _int("PORT", 5000)
        # 版本号随里程碑前进：M2 = 0.2.0，M3 = 0.3.0，以此类推
        self.version = _str("APP_VERSION", "0.3.0")

        # --- 数据库 ---
        self.database_url = _str("DATABASE_URL", "sqlite:///./data/app.db")

        # --- 鉴权 ---
        self.jwt_secret = _str("JWT_SECRET", "")
        self.jwt_algorithm = "HS256"
        self.jwt_expire_minutes = _int("JWT_EXPIRE_MINUTES", 720)
        self.secret_warnings: list[str] = []

        # --- 文件 ---
        self.upload_dir = _abs_path(_str("UPLOAD_DIR", "./data/uploads"))
        self.max_upload_mb = _int("MAX_UPLOAD_MB", 20)
        self.data_dir = _abs_path(_str("DATA_DIR", "./data"))

        # --- 模型接入（LLM 留空即降级模式）---
        self.llm_base_url = _str("LLM_BASE_URL", "")
        self.llm_api_key = _str("LLM_API_KEY", "")
        self.llm_model = _str("LLM_MODEL", "")
        self.llm_timeout_seconds = _int("LLM_TIMEOUT_SECONDS", 30)
        self.llm_temperature = _float("LLM_TEMPERATURE", 0.2)
        self.llm_max_retry = _int("LLM_MAX_RETRY", 1)

        # --- Embedding ---
        self.embedding_base_url = _str("EMBEDDING_BASE_URL", "")
        self.embedding_api_key = _str("EMBEDDING_API_KEY", "")
        self.embedding_model = _str("EMBEDDING_MODEL", "")

        # --- 检索与切分 ---
        self.rag_top_k = _int("RAG_TOP_K", 6)
        # 拒答阈值。注意：M3 实测证明**原始 BM25 分数不可用作阈值**（无关查询可能比相关查询分更高，
        # 见 app/indexing.py:search 文档串与 tests/test_indexing.py），因此 M4 里这一项只会比对
        # "归一化后的分数"，不直接作用于原始 BM25 分。
        self.rag_min_score = _float("RAG_MIN_SCORE", 0.35)
        # 检索粗筛：命中的查询词项占比低于此值视为"主题不相关"，直接不进 LLM。
        # 0.6 是 3 片段 toy 语料上观察到的分界（相关≥0.67 / 无关≤0.5），**未经评测集校准**，M4 必须重新定标。
        self.rag_min_coverage = _float("RAG_MIN_COVERAGE", 0.6)
        self.chunk_size = _int("CHUNK_SIZE", 600)
        self.chunk_overlap = _int("CHUNK_OVERLAP", 80)
        # 有效文本阈值：低于此字符数判 empty（系统设计说明书 §4.2）
        self.min_text_chars = _int("MIN_TEXT_CHARS", 50)
        # PDF 每页平均字符数低于此值提示"疑似扫描件"
        self.scanned_page_char_threshold = _int("SCANNED_PAGE_CHAR_THRESHOLD", 20)

        # --- 其它 ---
        self.rate_limit_per_minute = _int("RATE_LIMIT_PER_MINUTE", 20)
        # 登录尝试上限（进程内计数）。设为 0 或负数即关闭——测试里这么用，
        # 否则几十个用例共用同一个"IP"，会把限流当成 bug 查
        self.login_rate_limit_per_minute = _int("LOGIN_RATE_LIMIT_PER_MINUTE", 20)
        self.demo_banner = _bool("DEMO_BANNER", True)

        if overrides:
            for key, value in overrides.items():
                setattr(self, key, value)

        # 覆盖后再做一次归一化与校验，保证测试覆写的 database_url / upload_dir 同样生效
        self.database_url = self._normalize_db_url(self.database_url)
        self.upload_dir = _abs_path(str(self.upload_dir))
        self.data_dir = _abs_path(str(self.data_dir))

        if not self.jwt_secret:
            # 不静默生成随机值：那会让重启后所有令牌失效且难以排查
            self.jwt_secret = "dev-only-insecure-secret-change-me"
            self.secret_warnings.append(
                "JWT_SECRET 未设置，已使用开发用临时值；请在 .env 中填入随机密钥"
            )
        elif self.jwt_secret == PLACEHOLDER_SECRET:
            self.secret_warnings.append(
                "JWT_SECRET 仍是 .env.example 的占位符，部署前必须更换："
                'python -c "import secrets;print(secrets.token_hex(32))"'
            )

    # ---------- 派生属性 ----------
    @staticmethod
    def _normalize_db_url(url: str) -> str:
        """把 sqlite 相对路径钉到项目根目录，避免受启动时工作目录影响。"""
        prefix = "sqlite:///"
        if not url.startswith(prefix):
            return url
        raw_path = url[len(prefix):]
        if raw_path in ("", ":memory:") or raw_path.startswith(":memory:"):
            return url
        path = Path(raw_path)
        if not path.is_absolute():
            path = (PROJECT_ROOT / path).resolve()
        return prefix + path.as_posix()

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_api_key and self.llm_model)

    @property
    def llm_mode(self) -> str:
        return "llm" if self.llm_configured else "degraded"

    @property
    def embedding_mode(self) -> str:
        if self.embedding_api_key and self.embedding_model:
            return "vector"
        return "bm25_only" if self.llm_configured else "disabled"

    @property
    def sqlite_path(self) -> Path | None:
        prefix = "sqlite:///"
        if not self.database_url.startswith(prefix):
            return None
        raw = self.database_url[len(prefix):]
        return None if ":memory:" in raw else Path(raw)

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.upload_dir.mkdir(parents=True, exist_ok=True)


# 进程级默认实例。create_app() 若收到 overrides 会另建一个，不污染这个。
settings = Settings()
