"""评测集跑批（N-02）。24 条 × 6 类，真实调用模型。

为什么要这个脚本，而不是用 pytest 兼做：
- pytest 的断言是"代码行为对不对"；这里判的是"回答质量够不够"，性质不同。
- 改一版 Prompt 之后，需要一条命令回答"是变好了还是变坏了"，以及**具体哪几条退化了**。
  这需要有基准数（本文件读 `eval/qa_set.jsonl`），而不是每次重新拍脑袋。

判定口径（不模糊处理，写死在下面 JUDGE 里）：
- expect=answer：`code==0` 且答案命中 must_contain 中**至少一个**关键词。
  用"至少一个"而非"全部"，是因为同一个意思模型有无数种措辞，
  全命中会把"答对了但换了说法"判成失败，从而误导调优方向。
- expect=refuse：`code ∈ {4001, 4003, 4006}`。这三种都是"没给出无依据答案"。
- 引用越界：任何 `code==0` 的响应的 citations 里的 chunk_id 必须真实存在于本次索引。
  这是硬门禁，与类别无关。

用法：
    python tools/run_eval.py                      # 用 .env 里的阈值跑一遍
    python tools/run_eval.py --min-coverage 0.34  # 覆盖阈值（用于定标扫描）
    python tools/run_eval.py --sweep              # 扫多个阈值，输出对比表
    python tools/run_eval.py --out eval/last.json # 存原始结果便于逐条复核
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import tempfile
import time
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SET_PATH = os.path.join(ROOT, "eval", "qa_set.jsonl")
DEMO_FILES = [
    "01-本科生奖学金评审办法（2026年版）.pdf",
    "02-学生事务办理指南.docx",
]
REFUSE_CODES = {4001, 4003, 4006}
CATEGORY_ORDER = ["A", "B", "C", "D", "E", "F"]


def load_cases() -> list[dict]:
    cases = []
    with io.open(SET_PATH, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    return cases


def build_env() -> str:
    """独立临时库：反复跑不会撞 SHA-256 去重，也不会污染 data/app.db。"""
    workdir = tempfile.mkdtemp(prefix="campus-eval-")
    os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(workdir, "app.db").replace("\\", "/")
    os.environ["UPLOAD_DIR"] = os.path.join(workdir, "uploads")
    os.environ["LOGIN_RATE_LIMIT_PER_MINUTE"] = "0"
    return workdir


def prepare_app(min_coverage: float | None):
    from app import create_app

    overrides = {"login_rate_limit_per_minute": 0}
    if min_coverage is not None:
        overrides["rag_min_coverage"] = min_coverage
    app = create_app(overrides)

    with app.app_context():
        from app.auth import hash_password
        from app.contract import new_ulid
        from app.models import DocumentChunk, User, db

        db.create_all()
        db.session.add(User(id=new_ulid(), username="admin", display_name="评测管理员",
                            password_hash=hash_password("admin123"), role="admin"))
        db.session.commit()

    client = app.test_client()
    token = client.post("/api/v1/auth/login",
                        json={"username": "admin", "password": "admin123"}).get_json()["data"]["token"]
    headers = {"Authorization": f"Bearer {token}"}

    for name in DEMO_FILES:
        path = os.path.join(ROOT, "demo", name)
        if not os.path.exists(path):
            # 文件名改过一次（去掉「（演示样例）」），这里写死过一次旧名，
            # 结果评测直接 FileNotFoundError，而不是"某条答错"。
            # 报错时把 demo/ 里真实存在的文件列出来，下次一眼看出该改成什么。
            available = sorted(
                p.name for p in (ROOT / "demo").iterdir() if p.is_file()
            )
            raise SystemExit(f"评测要用的示例文件不存在：{name}\ndemo/ 现有：{available}")
        with open(path, "rb") as handle:
            resp = client.post("/api/v1/documents", headers=headers,
                               data={"file": (io.BytesIO(handle.read()), name)},
                               content_type="multipart/form-data")
        if resp.status_code != 201:
            raise RuntimeError(f"上传失败 {name}: {resp.status_code} {resp.get_json()}")

    with app.app_context():
        from app.indexing import rebuild_index
        from app.models import DocumentChunk, db as _db
        rebuild_index()
        known_ids = {row[0] for row in _db.session.execute(_db.select(DocumentChunk.id)).all()}

    return client, headers, known_ids


def judge(case: dict, body: dict, known_ids: set[str]) -> tuple[bool, str]:
    code = body.get("code")
    data = body.get("data") or {}

    if case["expect"] == "refuse":
        if code in REFUSE_CODES:
            return True, f"code={code}"
        return False, f"应拒答但 code={code}，返回了答案：{(data.get('answer') or '')[:60]}"

    # expect == answer
    if code != 0:
        return False, f"应回答但 code={code}：{(body.get('message') or '')[:70]}"
    answer = data.get("answer") or ""
    cites = data.get("citations") or []
    if not cites:
        return False, "返回了答案但没有任何引用"
    bad = [c["chunk_id"] for c in cites if c["chunk_id"] not in known_ids]
    if bad:
        return False, f"引用越界：{bad}"
    wanted = case.get("must_contain") or []
    if wanted and not any(word in answer for word in wanted):
        return False, f"关键词未命中（期望其一：{wanted}）"
    return True, f"引用 {[c['index'] for c in cites]}"


def run_once(min_coverage: float | None, cases: list[dict], verbose: bool) -> dict:
    workdir = build_env()
    client, headers, known_ids = prepare_app(min_coverage)

    rows = []
    for case in cases:
        started = time.time()
        resp = client.post("/api/v1/qa/ask", headers=headers, json={"question": case["question"]})
        body = resp.get_json()
        elapsed = int((time.time() - started) * 1000)
        ok, reason = judge(case, body, known_ids)
        rows.append({**case, "ok": ok, "reason": reason, "http": resp.status_code,
                     "code": body.get("code"), "ms": elapsed,
                     "answer": (body.get("data") or {}).get("answer")})
        if verbose:
            print(f"  {'PASS' if ok else 'FAIL'}  {case['id']:5} {elapsed:5}ms  {reason}")

    by_category: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_category[row["category"]].append(row)

    stats = {}
    for category in CATEGORY_ORDER:
        group = by_category.get(category, [])
        if group:
            passed = sum(1 for r in group if r["ok"])
            stats[category] = {
                "name": group[0]["category_name"],
                "total": len(group), "passed": passed,
                "rate": round(passed / len(group), 3),
            }

    # 引用越界是硬门禁，单独统计（judge 已把它算进 ok，这里再明确一次）
    citation_violations = sum(1 for r in rows if "引用越界" in r["reason"])
    refuse_group = by_category.get("B", [])
    refuse_rate = (sum(1 for r in refuse_group if r["ok"]) / len(refuse_group)) if refuse_group else 0.0

    return {
        "min_coverage": min_coverage,
        "total": len(rows), "passed": sum(1 for r in rows if r["ok"]),
        "stats": stats, "rows": rows,
        "citation_violations": citation_violations,
        "refuse_rate_b": round(refuse_rate, 3),
        "workdir": workdir,
    }


def print_report(result: dict, show_failures: bool = True) -> None:
    print()
    print("=" * 78)
    print(f"评测结果（RAG_MIN_COVERAGE={result['min_coverage']}）")
    print("=" * 78)
    print(f"{'类':3} {'类别':16} {'通过':>5} {'总数':>5} {'通过率':>7}")
    for code in CATEGORY_ORDER:
        item = result["stats"].get(code)
        if item:
            print(f"{code:3} {item['name']:16} {item['passed']:>5} {item['total']:>5} "
                  f"{item['rate'] * 100:>6.1f}%")
    total, passed = result["total"], result["passed"]
    print("-" * 78)
    print(f"{'合计':3} {'':16} {passed:>5} {total:>5} {passed / total * 100:>6.1f}%")
    print(f"B 类拒答正确率：{result['refuse_rate_b'] * 100:.1f}%  "
          f"（门禁 ≥ 90%）  引用越界：{result['citation_violations']} 处  （门禁 = 0）")

    if show_failures:
        failures = [r for r in result["rows"] if not r["ok"]]
        if failures:
            print()
            print("失败明细：")
            for row in failures:
                print(f"  {row['id']}  [{row['category_name']}]  {row['question'][:40]}")
                print(f"        → {row['reason']}")
    print("=" * 78)


def main() -> int:
    parser = argparse.ArgumentParser(description="跑 24 条评测集")
    parser.add_argument("--min-coverage", type=float, default=None,
                        help="覆盖 RAG_MIN_COVERAGE，用于阈值定标")
    parser.add_argument("--sweep", action="store_true", help="扫描多个阈值并对比")
    parser.add_argument("--thresholds", default="0.2,0.34,0.45,0.6",
                        help="--sweep 时扫描的阈值列表，逗号分隔")
    parser.add_argument("--out", default=None, help="把原始结果写到 JSON 文件")
    parser.add_argument("--quiet", action="store_true", help="只打印汇总，不逐条打印")
    args = parser.parse_args()

    cases = load_cases()
    print(f"载入评测集 {len(cases)} 条，分布：", end="")
    counter: dict[str, int] = defaultdict(int)
    for case in cases:
        counter[case["category"]] += 1
    print(" / ".join(f"{c}{counter[c]}" for c in CATEGORY_ORDER))

    if args.sweep:
        thresholds = [float(x) for x in args.thresholds.split(",") if x.strip()]
        summary = []
        for threshold in thresholds:
            print(f"\n[扫描] RAG_MIN_COVERAGE={threshold} …")
            result = run_once(threshold, cases, verbose=False)
            summary.append(result)
        print()
        print("=" * 78)
        print("阈值定标对比")
        print("=" * 78)
        print(f"{'阈值':>6} {'总通过率':>9} {'B类拒答':>8} {'A/C/D/E 回答':>13} {'引用越界':>9}")
        for result in summary:
            answerable = [result["stats"][c] for c in ("A", "C", "D", "E") if c in result["stats"]]
            hit = sum(item["passed"] for item in answerable)
            tot = sum(item["total"] for item in answerable)
            print(f"{result['min_coverage']:>6} {result['passed'] / result['total'] * 100:>8.1f}% "
                  f"{result['refuse_rate_b'] * 100:>7.1f}% {hit}/{tot:<11} "
                  f"{result['citation_violations']:>9}")
        print("=" * 78)
        if args.out:
            with io.open(args.out, "w", encoding="utf-8") as handle:
                json.dump([{k: v for k, v in r.items() if k != "rows"} for r in summary],
                          handle, ensure_ascii=False, indent=2)
        return 0

    result = run_once(args.min_coverage, cases, verbose=not args.quiet)
    print_report(result)

    if args.out:
        with io.open(args.out, "w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
        print(f"原始结果已写入 {args.out}")

    gate_b = result["refuse_rate_b"] >= 0.9
    gate_citation = result["citation_violations"] == 0
    print(f"门禁结论：B 类拒答≥90% → {'通过' if gate_b else '未通过'}；"
          f"引用越界=0 → {'通过' if gate_citation else '未通过'}")
    print(f"临时库：{result['workdir']}")
    return 0 if (gate_b and gate_citation) else 1


if __name__ == "__main__":
    sys.exit(main())
