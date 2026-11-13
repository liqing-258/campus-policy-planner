"""逐份文件跑「继续追问」：每一条派生建议都必须答得出。

**它守的是一条可断言的契约，不是一次抽查**

R5 起，界面上的三条「继续追问」由后端按「刚答的文件 + 它真实存在的章节」派生
（`app/qa.py::build_next_questions`）。这份契约的判据很硬：

    /qa/ask 返回的 next_questions 里，每一条再次提交都必须 code=0（给出答案）。

改前不是这样——前端按意图标签造句（`{label}需要准备哪些材料？`），
18 份文件 × 3 条里 9 条必拒答，而**没有任何脚本会发现**：它们看起来都是"合理的追问"。

**为什么按文件跑而不是只跑评测集**

24 条评测集按**题型**覆盖（能答 / 应拒 / 组合 / 近义 / 引用边界 / 注入），
不回答"这 18 份文件每一份的追问都能不能答"。R2 扩语料时漏掉的那类问题
（某些章节被挤出证据窗口）也是靠按文件跑才看见的。

**跑法与清理**

    .venv\\Scripts\\python.exe tools\\followup_probe.py
    .venv\\Scripts\\python.exe tools\\followup_probe.py --clean      # 跑完删掉自己产生的会话
    .venv\\Scripts\\python.exe tools\\followup_probe.py --out .workbuddy/followup.json
    .venv\\Scripts\\python.exe tools\\followup_probe.py --max-per-doc 3

要服务已经在跑（默认 `http://127.0.0.1:5000`），且 `.env` 里有模型 Key——
追问的"答得出"是模型行为，不能靠 mock 判。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

from kb_probe import CASES  # noqa: E402  同一份"文件 → 一个问题"的种子，别维护两套

DB = ROOT / "data" / "app.db"


@dataclass
class Row:
    doc: str
    question: str
    code: int
    answer_chars: int = 0
    citations: int = 0
    seconds: float = 0.0
    error: str = ""


def _call(base: str, path: str, body: dict | None, token: str | None) -> dict:
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        method="POST" if body is not None else "GET",
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.load(resp)


def _login(base: str, username: str, password: str) -> str:
    return _call(base, "/api/v1/auth/login",
                 {"username": username, "password": password}, None)["data"]["token"]


def _ask(base: str, token: str, question: str, conversation_id: str | None) -> dict:
    body: dict = {"question": question}
    if conversation_id:
        body["conversation_id"] = conversation_id
    return _call(base, "/api/v1/qa/ask", body, token)


def cleanup(titles: list[str], since: str) -> int:
    """删掉本次探针写进问答历史的会话（按标题 + 起始时间双重限定）。"""
    if not DB.is_file():
        return 0
    con = sqlite3.connect(DB)
    try:
        marks = ",".join("?" * len(titles))
        ids = [r[0] for r in con.execute(
            f"select id from conversations where title in ({marks}) and created_at >= ?",
            (*titles, since))]
        if ids:
            ph = ",".join("?" * len(ids))
            con.execute(f"delete from messages where conversation_id in ({ph})", ids)
            con.execute(f"delete from conversations where id in ({ph})", ids)
            con.commit()
        print(f"[清理] 已删 {len(ids)} 条探针会话")
        return len(ids)
    finally:
        con.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="逐份文件验证「继续追问」都可答")
    parser.add_argument("base", nargs="?", default="http://127.0.0.1:5000")
    parser.add_argument("--user", default="student")
    parser.add_argument("--password", default="student123")
    parser.add_argument("--max-per-doc", type=int, default=3)
    parser.add_argument("--out", default="")
    parser.add_argument("--clean", action="store_true", help="跑完删除本次产生的会话")
    args = parser.parse_args()

    base = args.base.rstrip("/")
    since = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    token = _login(base, args.user, args.password)

    rows: list[Row] = []
    titles: list[str] = []
    for index, (doc, seed) in enumerate(CASES, 1):
        try:
            first = _ask(base, token, seed, None)
        except urllib.error.URLError as exc:
            print(f"[{index:2d}] 首问失败：{exc!r}")
            return 1
        data = first.get("data") or {}
        conv = data.get("conversation_id")
        titles.append(seed.strip()[:30])
        questions = (data.get("next_questions") or [])[: args.max_per_doc]
        print(f"{index:2d} {doc[:18]:20s} 首问 {first.get('code')} · 建议 {len(questions)} 条")
        if not questions:
            rows.append(Row(doc, "（没有派生建议）", -1, error="next_questions 为空"))
            continue
        for question in questions:
            started = time.perf_counter()
            try:
                res = _ask(base, token, question, conv)
                payload = res.get("data") or {}
                conv = payload.get("conversation_id") or conv
                row = Row(
                    doc=doc,
                    question=question,
                    code=int(res.get("code") or 0),
                    answer_chars=len((payload.get("answer") or "").strip()),
                    citations=len(payload.get("citations") or []),
                    seconds=time.perf_counter() - started,
                )
            except urllib.error.URLError as exc:
                row = Row(doc, question, -1, seconds=time.perf_counter() - started,
                          error=repr(exc))
            rows.append(row)
            ok = row.code == 0 and row.answer_chars > 0 and row.citations > 0
            mark = "OK  " if ok else f"FAIL({row.code})"
            print(f"    {mark} {row.seconds:4.1f}s {row.answer_chars:4d} 字 "
                  f"/ {row.citations} 引用 · {question[:34]}")

    failed = [r for r in rows if not (r.code == 0 and r.answer_chars > 0 and r.citations > 0)]
    print(f"\n合计 {len(rows)} 条建议，可答 {len(rows) - len(failed)}，失败 {len(failed)}")
    for row in failed:
        print(f"  [{row.code}] {row.doc} | {row.question} | {row.error or '无答案或无引用'}")

    if args.out:
        Path(args.out).write_text(
            json.dumps([row.__dict__ for row in rows], ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"[保存] {args.out}")

    if args.clean:
        cleanup(titles, since)

    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
