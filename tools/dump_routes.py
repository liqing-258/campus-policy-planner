"""导出真实路由表，用于复核文档里的接口数（禁止手填）。

用法：
    .venv/Scripts/python.exe tools/dump_routes.py
    .venv/Scripts/python.exe tools/dump_routes.py --json .workbuddy/routes.json
    .venv/Scripts/python.exe tools/dump_routes.py --expect 30   # 不符则退出码 1

为什么要这个脚本：旧项目踩过"文档写 29 条、代码 31 条"的坑（风险 R-11），
接口数只允许从 `app.url_map` 导出，不允许数表格行。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import create_app, dump_routes  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="导出路由表")
    parser.add_argument("--json", dest="json_path", default=None, help="同时写入 JSON 文件")
    parser.add_argument("--prefix", default="/api/v1", help="只统计该前缀下的路由")
    parser.add_argument("--expect", type=int, default=None, help="期望条数，不符则失败")
    args = parser.parse_args()

    app = create_app()
    rows = [r for r in dump_routes(app) if r["rule"].startswith(args.prefix)]

    by_method: dict[str, int] = {}
    for row in rows:
        by_method[row["method"]] = by_method.get(row["method"], 0) + 1

    width = max((len(r["rule"]) for r in rows), default=10)
    print(f"{'METHOD':7} {'RULE':{width}}  ENDPOINT")
    print("-" * (width + 30))
    for row in rows:
        print(f"{row['method']:7} {row['rule']:{width}}  {row['endpoint']}")

    print()
    print(f"合计 {len(rows)} 条（前缀 {args.prefix}）")
    print("按方法：" + " / ".join(f"{m} {n}" for m, n in sorted(by_method.items())))

    if args.json_path:
        target = Path(args.json_path)
        if not target.is_absolute():
            target = ROOT / target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({"total": len(rows), "by_method": by_method, "routes": rows},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"已写入 {target}")

    if args.expect is not None and len(rows) != args.expect:
        print(f"[fail] 实际 {len(rows)} 条，期望 {args.expect} 条")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
