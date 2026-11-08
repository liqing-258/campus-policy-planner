"""种子数据脚本：写入两个演示账号。

**全部虚构**（红线 X-03）：这些不是真实学生，密码也是公开的演示密码，
`docs/known-limitations.md` 中已声明。任何真实学号/姓名/成绩都不得出现在本文件。

用法：
    .venv\\Scripts\\python.exe tools\\seed_demo.py            # 幂等写入
    .venv\\Scripts\\python.exe tools\\seed_demo.py --reset    # 先清空这两个账号再写
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from app.auth import hash_password  # noqa: E402
from app.contract import new_ulid  # noqa: E402
from app.models import User, db  # noqa: E402
from shared.enums import ROLE_ADMIN, ROLE_STUDENT  # noqa: E402

# 用户名 / 密码 / 显示名 / 角色 —— 全部为演示虚构数据
DEMO_USERS = (
    ("student", "student123", "演示学生", ROLE_STUDENT),
    ("admin", "admin123", "演示管理员", ROLE_ADMIN),
)


def main() -> int:
    parser = argparse.ArgumentParser(description="写入演示账号")
    parser.add_argument("--reset", action="store_true", help="先删除同名账号")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        for username, password, display_name, role in DEMO_USERS:
            existing = db.session.execute(
                db.select(User).where(User.username == username)
            ).scalar_one_or_none()
            if existing is not None:
                if args.reset:
                    db.session.delete(existing)
                    db.session.flush()
                else:
                    if not args.quiet:
                        print(f"[skip] {username} 已存在（role={existing.role}）")
                    continue
            db.session.add(User(
                id=new_ulid(),
                username=username,
                display_name=display_name,
                password_hash=hash_password(password),
                role=role,
            ))
            print(f"[add ] {username:8} role={role:8} password={password}")
        db.session.commit()

        total = db.session.query(User).count()
        print(f"[ok] 用户总数：{total}")
        if total < len(DEMO_USERS):
            print("[fail] 演示账号数量不足")
            return 1
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
