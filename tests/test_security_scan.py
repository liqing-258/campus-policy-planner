"""M2 门禁："无密钥入库" 的自动化兜底（同时对应 T-24）。

为什么做成测试而不是人工看一遍：令牌泄露过一次（R-05），
人工检查会漏，而这条断言每次 `pytest` 都会重跑。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "data",
             ".pytest_cache", ".workbuddy", "dist", "build", ".vite"}
SKIP_SUFFIX = {".db", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2",
               ".pdf", ".docx", ".zip", ".lock"}

# 真实密钥的形态特征（占位符与示例不算）
PATTERNS = {
    "GitHub PAT": re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    "GitHub fine-grained": re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    "OpenAI/兼容 key": re.compile(r"\bsk-[A-Za-z0-9]{24,}"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "私钥文件头": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}

# 允许出现的占位写法
ALLOWLIST = (
    "please-replace",
    "your-key",
    "<token>",
    "xxx",
    "示例",
    "placeholder",
)


def _iter_text_files():
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in SKIP_SUFFIX:
            continue
        if path.name == ".env":
            continue  # 本地配置，且必须处于未跟踪状态（下一条用例校验）
        yield path


def test_no_hardcoded_credentials_in_repository_files():
    hits: list[str] = []
    for path in _iter_text_files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for name, pattern in PATTERNS.items():
            for match in pattern.finditer(text):
                snippet = text[max(0, match.start() - 30):match.end()].lower()
                if any(token in snippet for token in ALLOWLIST):
                    continue
                hits.append(f"{name} @ {path.relative_to(ROOT)}: {match.group(0)[:12]}…")
    assert not hits, "疑似密钥入库：\n" + "\n".join(hits)


def test_env_file_exists_but_is_gitignored():
    env_path = ROOT / ".env"
    if not env_path.exists():
        return  # 新克隆的仓库里本来就没有 .env，属正常
    result = subprocess.run(["git", "check-ignore", "-q", ".env"], cwd=ROOT)
    assert result.returncode == 0, ".env 未被 .gitignore 忽略，存在入库风险"


def test_env_is_not_tracked():
    if not (ROOT / ".git").exists():
        return
    result = subprocess.run(["git", "ls-files", "--error-unmatch", ".env"],
                            cwd=ROOT, capture_output=True)
    assert result.returncode != 0, ".env 已被 Git 跟踪，必须立刻移出版本库"


def test_env_example_contains_no_real_secret():
    """模板文件可以入库，但里面的值必须是占位符。"""
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "ghp_" not in text
    assert not PATTERNS["OpenAI/兼容 key"].search(text)
    assert "please-replace" in text or "JWT_SECRET=" in text
