"""环境自检：新机器上第一次跑之前，先跑这个。

设计目的：把"README 里写了但没验证"的隐性前提显式化为可执行检查。
任何一项 FAIL 都给出**具体修法**，而不是只说"环境有问题"。

用法：
    .venv\\Scripts\\python.exe tools\\selfcheck.py
    .venv\\Scripts\\python.exe tools\\selfcheck.py --strict   # 有 WARN 也返回 1
"""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata as metadata
import os
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

results: list[tuple[str, str, str]] = []  # (level, name, detail)


def check(level: str, name: str, detail: str) -> None:
    results.append((level, name, detail))


def _pkg(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    # 1. Python 版本
    if sys.version_info >= (3, 11):
        check("PASS", "Python 版本", f"{sys.version.split()[0]}（要求 ≥ 3.11）")
    else:
        check("FAIL", "Python 版本", f"{sys.version.split()[0]}，请升级到 3.11+")

    # 2. 关键依赖
    for pkg in ("Flask", "Flask-SQLAlchemy", "SQLAlchemy", "pydantic", "PyJWT",
                "PyMuPDF", "python-docx", "jieba", "numpy", "httpx", "waitress"):
        version = _pkg(pkg)
        if version:
            check("PASS", f"依赖 {pkg}", version)
        else:
            check("FAIL", f"依赖 {pkg}", "未安装 → pip install -r requirements.txt")

    # 3. .env
    env_file = ROOT / ".env"
    if env_file.exists():
        text = env_file.read_text(encoding="utf-8", errors="ignore")
        if "please-replace-with-a-random-32-byte-hex" in text:
            check("WARN", ".env", "JWT_SECRET 仍是占位符，正式演示前请更换")
        elif "JWT_SECRET=" not in text:
            check("WARN", ".env", "缺少 JWT_SECRET，将使用开发临时密钥")
        else:
            check("PASS", ".env", "存在且已配置 JWT_SECRET")
    else:
        check("WARN", ".env", "不存在 → copy .env.example .env（会自动生成开发用密钥）")

    # 4. 目录
    for name in ("data", "data/uploads"):
        path = ROOT / name
        check("PASS" if path.is_dir() else "WARN", f"目录 {name}",
              "存在" if path.is_dir() else "缺失（启动时会自动创建）")

    # 5. 数据库与表
    try:
        from app import create_app
        from app.models import ALL_MODELS, User, db
        from sqlalchemy import inspect

        app = create_app()
        with app.app_context():
            tables = set(inspect(db.engine).get_table_names())
            expected = {m.__tablename__ for m in ALL_MODELS}
            missing = expected - tables
            if missing:
                check("FAIL", "数据表", f"缺少 {len(missing)} 张：{sorted(missing)} → 运行 tools/init_db.py")
            else:
                check("PASS", "数据表", f"{len(expected)} 张齐备")
            users = db.session.query(User).count()
            if users >= 2:
                check("PASS", "演示账号", f"{users} 个")
            else:
                check("WARN", "演示账号", f"仅 {users} 个 → 运行 tools/seed_demo.py")
    except Exception as exc:  # 依赖缺失时不应整个脚本崩掉
        check("FAIL", "数据表", f"无法连接数据库：{exc}")

    # 6. .env 是否被 Git 跟踪
    if (ROOT / ".git").exists():
        tracked = subprocess.run(["git", "ls-files", "--error-unmatch", ".env"],
                                 cwd=ROOT, capture_output=True).returncode == 0
        if tracked:
            check("FAIL", "密钥安全", ".env 已被 Git 跟踪，必须 git rm --cached .env")
        else:
            check("PASS", "密钥安全", ".env 未被跟踪")

    # 7. 端口占用
    for port in (5000, 5173):
        with socket.socket() as sock:
            sock.settimeout(0.3)
            busy = sock.connect_ex(("127.0.0.1", port)) == 0
        check("WARN" if busy else "PASS", f"端口 {port}",
              "已被占用（若已有实例在跑则正常）" if busy else "空闲")

    # 输出
    icons = {"PASS": "[PASS]", "WARN": "[WARN]", "FAIL": "[FAIL]"}
    width = max(len(name) for _, name, _ in results)
    for level, name, detail in results:
        print(f"{icons[level]} {name:{width}}  {detail}")

    fails = sum(1 for lvl, _, _ in results if lvl == "FAIL")
    warns = sum(1 for lvl, _, _ in results if lvl == "WARN")
    print(f"\n合计 {len(results)} 项：通过 {len(results) - fails - warns} / 警告 {warns} / 失败 {fails}")
    if fails:
        print("结论：环境未就绪，按上面的提示修复后再启动。")
        return 1
    if warns and args.strict:
        print("结论：有警告，--strict 模式下视为未通过。")
        return 1
    print("结论：环境就绪，可运行 scripts/start-backend 与 scripts/start-frontend。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
