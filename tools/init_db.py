"""建表脚本：一条命令把 13 张表建好，并校验结果。

用法：
    .venv\\Scripts\\python.exe tools\\init_db.py           # 建表（已存在则跳过）
    .venv\\Scripts\\python.exe tools\\init_db.py --drop    # 删表重建（仅开发环境）

为什么不用 Alembic：11 天工期里迁移基建没有收益（数据模型文档 §1）。
M6 起如需结构变更再引入，届时 `create_all` 的产物可作为初始基线。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import inspect, text  # noqa: E402

from app import create_app  # noqa: E402
from app.models import ALL_MODELS, db  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="初始化数据库")
    parser.add_argument("--drop", action="store_true", help="先删除所有表（开发环境用）")
    parser.add_argument("--quiet", action="store_true", help="只输出结果行")
    args = parser.parse_args()

    app = create_app()
    settings = app.extensions["settings"]

    with app.app_context():
        inspector = inspect(db.engine)
        existing = set(inspector.get_table_names())
        if args.drop and existing:
            if not args.quiet:
                print(f"[drop] 删除 {len(existing)} 张表：{', '.join(sorted(existing))}")
            db.drop_all()

        db.create_all()

        tables = sorted(inspect(db.engine).get_table_names())
        expected = sorted(m.__tablename__ for m in ALL_MODELS)
        missing = [t for t in expected if t not in tables]
        extra = [t for t in tables if t not in expected and t != "sqlite_sequence"]

        # 外键在 SQLite 默认关闭，必须确认 PRAGMA 生效（否则级联删除静默失效）
        fk_on = db.session.execute(text("PRAGMA foreign_keys")).scalar()
        journal = db.session.execute(text("PRAGMA journal_mode")).scalar()

        if not args.quiet:
            print(f"[db] {settings.database_url}")
            print(f"[db] 表数量：{len(tables)} / 期望 {len(expected)}")
            print(f"[db] PRAGMA foreign_keys={fk_on}  journal_mode={journal}")
        print(f"[ok] 建表完成，共 {len(tables)} 张表")
        if missing:
            print(f"[fail] 缺少表：{missing}")
            return 1
        if extra:
            print(f"[warn] 多余表：{extra}")
        if str(fk_on) != "1":
            print("[fail] PRAGMA foreign_keys 未生效，级联删除会静默失败")
            return 1
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
