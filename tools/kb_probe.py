"""逐份政策各问一句：18 份文件，每一份都要能被问到、并引用回它自己。

为什么需要它：
评测集 24 条是按"题型"（A–F）覆盖的，界面巡检按"页面"覆盖的，
两者都不回答"这 18 份文件是不是**每一份**都能被问出来"。R2 扩写之后尤其需要这一条：
新加章节把高频道挤掉的副作用，正是靠这种逐份抽查才看得见。

**题目的来源有讲究**：必须从该文件的真实条款反推（原文写"门禁 23:30"，就问"门禁几点"），
不能凭想象问"宿舍几点熄灯"——原文没有熄灯，拒答是**正确行为**，却会被误记成缺陷。
第一版探针就是这么错的：18 题里有 6 题问的是库里没有的内容，得出"6/18 拒答"的假警报。

用法：
    .venv\\Scripts\\python.exe tools\\kb_probe.py
    .venv\\Scripts\\python.exe tools\\kb_probe.py --clean        # 跑完删掉自己产生的会话
    .venv\\Scripts\\python.exe tools\\kb_probe.py --out .workbuddy/kb_probe.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "app.db"

# （该问题由哪份文件回答，问法）
CASES: list[tuple[str, str]] = [
    ("学籍管理规定", "转专业要怎么申请？"),
    ("本科生奖学金评审办法（2026年版）", "国家励志奖学金的成绩要求是多少？"),
    ("本科生奖学金评审办法（2025年版）", "上一版办法里励志奖学金的绩点要求是几分？"),
    ("学生事务办理指南", "在读证明怎么办理？"),
    ("课程考核与成绩管理办法", "必修课的平时成绩占多少？"),
    ("推荐免试攻读研究生遴选办法", "推免要求学分绩点多少？"),
    ("学生考研与出国（境）交流支持办法", "考研自习区在哪里，怎么预约？"),
    ("国家助学贷款与勤工助学管理办法", "生源地贷款每年最多能贷多少？"),
    ("学生宿舍管理规定", "宿舍门禁是几点到几点？"),
    ("校园卡与网络服务使用指南", "补办校园卡要多少钱？"),
    ("图书馆借阅与自习空间管理办法", "本科生一次能借几本书、借多久？"),
    ("学科竞赛与创新创业项目管理规定", "A 类国家级一等奖奖励多少钱？"),
    ("全国大学英语四六级考试报名须知", "四级报名费多少？"),
    ("计算机等级考试与职业资格证书管理办法", "计算机二级报名费多少？"),
    ("学生社团与校园文化活动管理办法", "成立社团需要多少人？"),
    ("校医院就诊与心理健康服务指南", "校内看病报销比例是多少？"),
    ("食堂与后勤生活服务指南", "食堂食品安全问题打哪个电话？"),
    ("学生违纪处分与申诉管理办法", "警告处分期限是多久？"),
]


@dataclass
class Row:
    doc: str
    question: str
    refused: bool
    hit: bool
    cites: list[str]
    answer: str
    seconds: float


def post(base: str, path: str, payload: dict, token: str | None = None) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        base + path, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {token}"} if token else {})},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode())


def login(base: str, user: str, password: str) -> str:
    return post(base, "/api/v1/auth/login", {"username": user, "password": password})["data"]["token"]


def probe(base: str, token: str) -> list[Row]:
    rows: list[Row] = []
    for i, (doc, question) in enumerate(CASES, 1):
        started = time.time()
        try:
            data = post(base, "/api/v1/qa/ask", {"question": question}, token)["data"]
            refused = bool(data.get("refused"))
            cites = [(c.get("document_title") or "") for c in (data.get("citations") or [])]
            # 标题里带年份版本，取前 12 字比较，避免"（2026年版）"这种后缀干扰
            hit = any(doc[:12] in title for title in cites)
            answer = (data.get("answer") or "").replace("\n", " ")
        except (urllib.error.URLError, KeyError, ValueError) as exc:
            refused, cites, hit, answer = True, [], False, f"ERROR {exc!r}"
        row = Row(doc, question, refused, hit, cites, answer, time.time() - started)
        rows.append(row)
        mark = "REFUSED" if refused else ("OK  " if hit else "OTHER")
        print(f"{i:2d} {mark} {doc[:18]:20s} {question[:24]:26s} {row.seconds:5.1f}s")
    return rows


def cleanup(titles: list[str], since: str, dry_run: bool = False) -> int:
    """删掉本次探针写进问答历史的会话（按标题 + 起始时间双重限定）。"""
    if not DB.is_file():
        print(f"[清理] 找不到 {DB}")
        return 0
    con = sqlite3.connect(DB)
    try:
        marks = ",".join("?" * len(titles))
        ids = [r[0] for r in con.execute(
            f"select id from conversations where title in ({marks}) and created_at >= ?",
            (*titles, since))]
        if ids and not dry_run:
            ph = ",".join("?" * len(ids))
            con.execute(f"delete from messages where conversation_id in ({ph})", ids)
            con.execute(f"delete from conversations where id in ({ph})", ids)
            con.commit()
        print(f"[清理] {'将删' if dry_run else '已删'} {len(ids)} 条探针会话")
        return len(ids)
    finally:
        con.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="逐份政策可问性探针")
    parser.add_argument("--base", default="http://127.0.0.1:5000")
    parser.add_argument("--user", default="student")
    parser.add_argument("--password", default="student123")
    parser.add_argument("--out", default="", help="把结果写成 JSON")
    parser.add_argument("--clean", action="store_true", help="跑完删除本次产生的会话")
    args = parser.parse_args()

    # 库里 created_at 存的是 UTC（实测与本机 +8 差 8 小时），
    # 用本地时间去比会一条都匹配不上——第一版就是这样"清理了 0 条"。
    started_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    try:
        token = login(args.base, args.user, args.password)
    except (urllib.error.URLError, KeyError) as exc:
        print(f"登录失败：{exc!r}（服务起了吗？{args.base}）")
        return 1

    rows = probe(args.base, token)
    ok = sum(1 for r in rows if not r.refused and r.hit)
    refused = sum(1 for r in rows if r.refused)
    other = sum(1 for r in rows if not r.refused and not r.hit)
    print(f"\n逐份命中 {ok}/{len(rows)}；拒答 {refused}；引用别的文件 {other}")

    for r in rows:
        if r.refused or not r.hit:
            print(f"  - {r.doc} | {r.question} | refused={r.refused} | cites={r.cites[:3]}")
            if r.answer:
                print(f"    {r.answer[:80]}")

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(
            json.dumps([r.__dict__ for r in rows], ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[输出] {args.out}")

    if args.clean:
        cleanup([q for _, q in CASES], started_at)

    return 0 if ok == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
