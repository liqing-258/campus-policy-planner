"""重建 BM25 索引并打印统计。

用法：
    .venv\\Scripts\\python.exe tools\\rebuild_index.py
    .venv\\Scripts\\python.exe tools\\rebuild_index.py --probe "国家励志奖学金 申请条件"

`--probe` 会用给定问题跑一次检索，把 Top-3 片段打印出来。
它是 M3"索引真的可用"的可观测证据——不是靠"接口返回 200"来判断。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from app.indexing import get_index, rebuild_index  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="重建索引")
    parser.add_argument("--probe", default=None, help="用该问题试检索 Top-3")
    parser.add_argument("--top-k", type=int, default=3)
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        stats = rebuild_index()
        print(f"[index] 片段数   {stats['chunks']}")
        print(f"[index] 词项数   {stats['terms']}")
        print(f"[index] 平均词数 {stats['avg_chunk_terms']}")
        print(f"[index] 构建耗时 {stats['build_ms']} ms")

        if stats["chunks"] == 0:
            print("[warn] 没有任何片段，索引为空。请先上传文档或运行 tools\\load_demo_docs.py")

        if args.probe:
            print(f"\n[probe] 问题：{args.probe}")
            hits = get_index().search(args.probe, top_k=args.top_k)
            if not hits:
                print("  （无命中——若知识库确实没有相关内容，这正是期望行为）")
            for rank, hit in enumerate(hits, 1):
                where = f"第{hit.page_number}页" if hit.page_number else "无页码"
                print(f"  {rank}. score={hit.score:<7} coverage={hit.coverage:<6} "
                      f"{hit.document_title} / {where} / {hit.section_title or '无章节'}")
                print(f"     命中词项：{'、'.join(hit.matched_terms) or '—'}")
                print(f"     {hit.content[:70].replace(chr(10), ' ')}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
