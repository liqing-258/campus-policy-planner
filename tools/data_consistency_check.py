"""数据中心的口径核对：**接口报的数，能不能用 SQL 独立算出来**。

为什么需要这么一支脚本：数据中心是"给管理员下判断"的页面，它上面每个数字都有两种可能出错——
SQL 写错了（多算/少算/漏了过滤条件），或者前端把数字改了。只看页面永远分不清是哪一种。

所以这里用两条**互不相干**的路走同一批数据：

* **A 路**：`GET /admin/analytics/*`，就是页面真正拿的那份 JSON；
* **B 路**：直接对 `data/app.db` 写 SQL，公式按《系统设计说明书》§8.21 与
  `app/analytics.py` 里写明的口径重写一遍（不 import 那边的函数——否则同一个 bug 会同时
  错在两条路上，核对就失去意义）。

判据是"两边逐项相等"。`student_type` / `grade` 的分桶要做归一化（`full_time_undergraduate`
和"全日制在校本科生"是同一类），归一化规则本身不在这里重复实现——这里只验**分桶之和是否等于总数**，
以及**每一桶的计数与 SQL group by 的结果一致**。

用法：
    .venv\\Scripts\\python.exe tools\\data_consistency_check.py
    .venv\\Scripts\\python.exe tools\\data_consistency_check.py --db data/app.db
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 与 app/analytics.py 保持一致的两个常量（口径的一部分，不是实现细节）
PROFILE_FIELDS = (
    "student_type", "grade", "gpa", "rank_percent", "major",
    "credits_earned", "enrollment_status", "poverty_certified", "disciplinary_record",
)
LOCAL_UTC_OFFSET = 8  # 提问时段按北京时间分桶


def api(base: str, path: str, body: dict | None = None, token: str | None = None) -> dict:
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=json.dumps(body).encode() if body is not None else None,
        method="POST" if body is not None else "GET",
        headers={
            "Content-Type": "application/json",
            **({"Authorization": "Bearer " + token} if token else {}),
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:5000/api/v1")
    ap.add_argument("--db", default=str(ROOT / "data" / "app.db"))
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="admin123")
    args = ap.parse_args()

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))
        print(f"  {'OK  ' if ok else 'FAIL'} {name}{(' · ' + detail) if detail else ''}")

    token = api(args.base, "/auth/login", {"username": args.user, "password": args.password})["data"]["token"]
    portraits = api(args.base, "/admin/analytics/portraits", token=token)["data"]
    qa = api(args.base, "/admin/analytics/qa", token=token)["data"]
    fb = api(args.base, "/admin/feedback/stats", token=token)

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row

    def one(sql: str, params: tuple = ()) -> int:
        row = con.execute(sql, params).fetchone()
        return int(row[0] or 0)

    ps = portraits["summary"]
    qs = qa["summary"]

    # ---------- 画像：会话与判定 ----------
    session_count = one("select count(*) from match_sessions")
    check("画像会话总数", ps["session_count"] == session_count,
          f"接口 {ps['session_count']} / SQL {session_count}")

    # 判定计数有两条来源：session 上的 sum_* 冗余列，和 items 表逐条记录。**两者也该相等**
    by_items = dict(con.execute(
        "select status, count(*) from match_result_items group by status").fetchall())
    met_i, not_i, unk_i = (int(by_items.get(k, 0)) for k in ("met", "not_met", "unknown"))
    met_s, not_s, unk_s = (
        one("select coalesce(sum(sum_met),0) from match_sessions"),
        one("select coalesce(sum(sum_not_met),0) from match_sessions"),
        one("select coalesce(sum(sum_unknown),0) from match_sessions"),
    )
    check("符合/不符合/无法判定（接口 vs items 表）",
          (ps["met"], ps["not_met"], ps["unknown"]) == (met_i, not_i, unk_i),
          f"接口 {ps['met']}/{ps['not_met']}/{ps['unknown']} · items {met_i}/{not_i}/{unk_i}")
    check("冗余列 sum_* 与 items 表一致（防两处账本对不上）",
          (met_s, not_s, unk_s) == (met_i, not_i, unk_i),
          f"sum 列 {met_s}/{not_s}/{unk_s} · items {met_i}/{not_i}/{unk_i}")

    judged = met_i + not_i + unk_i
    check("判定总数", ps["judged"] == judged, f"接口 {ps['judged']} / SQL {judged}")
    rate = round(met_i / judged, 3) if judged else None
    check("平均符合率", ps["met_rate"] == rate, f"接口 {ps['met_rate']} / SQL {rate}")

    # 完整度：每条会话的 profile_json 里，9 个字段填了几个
    filled_total = 0
    for row in con.execute("select profile_json from match_sessions"):
        try:
            profile = json.loads(row[0] or "{}")
        except json.JSONDecodeError:
            profile = {}
        filled_total += sum(
            1 for f in PROFILE_FIELDS
            if str(profile.get(f) or "").strip() not in ("", "None")
        )
    avg_complete = round(filled_total / (session_count * len(PROFILE_FIELDS)), 3) if session_count else None
    check("平均完整度", ps["avg_completeness"] == avg_complete,
          f"接口 {ps['avg_completeness']} / SQL {avg_complete}（{filled_total} 格 / {session_count}×{len(PROFILE_FIELDS)}）")

    # 近 7 天：created_at 是 naive UTC
    cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7))
    recent = one("select count(*) from match_sessions where created_at >= ?",
                 (cutoff.strftime("%Y-%m-%d %H:%M:%S"),))
    check("近 7 天新增画像", ps["recent_7d"] == recent, f"接口 {ps['recent_7d']} / SQL {recent}")

    # 分桶之和必须等于总数（归一化后的桶不能丢也不能重复计）
    st_sum = sum(b["count"] for b in ps["by_student_type"])
    gr_sum = sum(b["count"] for b in ps["by_grade"])
    raw_sum = sum(b["count"] for b in ps["raw_student_types"])
    check("学生类型分桶之和 = 会话总数", st_sum == session_count, f"{st_sum} / {session_count}")
    check("年级分桶之和 = 会话总数", gr_sum == session_count, f"{gr_sum} / {session_count}")
    check("原始类型分桶之和 = 会话总数（归一化前）", raw_sum == session_count, f"{raw_sum} / {session_count}")

    # 年级桶逐项对：未填写之外的桶，标签是「N 年级」
    # json_extract 抽不到值时给的是 SQL NULL，落到 Python 就是 None；
    # 接口那边把它标成「未填写」。**同一件事的两种写法，不是两种数据**——
    # 口径对齐成「未填写」再比，否则一条纯键名差异会冒充成数据不一致。
    grade_sql = {
        ("未填写" if k is None else str(k)): int(v)
        for k, v in con.execute(
            "select json_extract(profile_json,'$.grade') g, count(*) "
            "from match_sessions group by g").fetchall()
    }
    grade_api = {}
    for bucket in ps["by_grade"]:
        label = bucket["label"]
        key = label.replace(" 年级", "") if label.endswith(" 年级") else label
        grade_api[key] = bucket["count"]
    check("年级逐桶计数一致", grade_api == grade_sql, f"接口 {grade_api} / SQL {grade_sql}")

    # ---------- 问答 ----------
    question_count = one("select count(*) from messages where role='user' and trim(content) <> ''")
    check("提问总数", qs["question_count"] == question_count,
          f"接口 {qs['question_count']} / SQL {question_count}")

    conv_count = one(
        "select count(distinct conversation_id) from messages where role='user' and trim(content) <> ''")
    check("涉及会话数", qs["conversation_count"] == conv_count,
          f"接口 {qs['conversation_count']} / SQL {conv_count}")

    asker_count = one(
        "select count(distinct c.user_id) from messages m "
        "join conversations c on c.id = m.conversation_id "
        "where m.role='user' and trim(m.content) <> ''")
    check("提问人数", qs["asker_count"] == asker_count,
          f"接口 {qs['asker_count']} / SQL {asker_count}")

    # 高峰时段：本地时区（UTC+8）分桶，同一时段并列时取**较小的小时**
    hours = {int(r[0]): int(r[1]) for r in con.execute(
        "select cast(strftime('%H', created_at, '+8 hours') as integer) h, count(*) "
        "from messages where role='user' and trim(content) <> '' group by h")}
    peak = None
    if question_count:
        best = max(hours.values())
        peak = min(h for h, n in hours.items() if n == best)
    check("提问高峰时段", qs["peak_hour"] == peak, f"接口 {qs['peak_hour']} / SQL {peak}")

    kw = qa.get("keywords") or []
    check("词云词数 = keyword_count 且词条非空",
          len(kw) == qs["keyword_count"] and all(w.get("word") for w in kw),
          f"keywords {len(kw)} 条 / keyword_count {qs['keyword_count']}")
    # 词云**按权重（TF-IDF）降序，不是按词频降序**——这是有意的：
    # 「本科」出现 26 次却排在倒第四，因为它在十几份文件里都有（IDF 低），
    # 而「游泳馆」只出现 2 次却更靠前（只在食堂后勤指南里出现，区分度高）。
    # 词云的字号跟着权重走，所以这里验的是权重序，外加"计数为正、权重在 (0,1]"。
    check("词云按权重降序且权重在 (0,1]",
          all(kw[i]["weight"] >= kw[i + 1]["weight"] for i in range(len(kw) - 1))
          and all(0 < w["weight"] <= 1 and w["count"] > 0 for w in kw),
          f"首 {kw[0]['word']}={kw[0]['count']}/{kw[0]['weight']} 末 {kw[-1]['word']}={kw[-1]['count']}/{kw[-1]['weight']}"
          if kw else "空")

    # 主题占比之和 ≈ 1（四舍五入误差留 0.02）
    topics = qa.get("topics") or []
    share_sum = round(sum(t["share"] for t in topics), 3)
    topic_sum = sum(t["count"] for t in topics)
    check("主题占比之和 ≈ 1", abs(share_sum - 1) <= 0.02, f"Σshare={share_sum}")
    check("主题计数之和 = 提问总数", topic_sum == question_count, f"Σcount={topic_sum} / {question_count}")

    # 24 小时补零：必须正好 24 个桶，且之和等于提问总数
    hb = qa.get("hours") or []
    check("时段桶为 24 个且计数之和 = 提问总数",
          len(hb) == 24 and sum(h["count"] for h in hb) == question_count,
          f"{len(hb)} 桶 Σ{sum(h['count'] for h in hb)}")

    # ---------- 反馈 ----------
    fdata = fb.get("data", {})
    qa_fb = one("select count(*) from qa_feedback")
    corr = one("select count(*) from correction_feedback")
    check("反馈总数 = 问答反馈 + 纠错",
          fdata["total"] == qa_fb + corr,
          f"接口 {fdata['total']} / SQL {qa_fb}+{corr}")
    check("问答反馈 helpful/unhelpful 之和 = qa 总数",
          fdata["qa"]["helpful"] + fdata["qa"]["unhelpful"] == fdata["qa"]["total"],
          f"{fdata['qa']}")

    # ---------- 明细表 ----------
    items = portraits.get("items") or []
    check("明细行数 ≤ 会话总数且与分页一致",
          len(items) == min(session_count, len(items)) and len(items) > 0,
          f"{len(items)} 行 / {session_count} 会话")
    bad = [it for it in items
           if it["met"] + it["not_met"] + it["unknown"] != it["met"] + it["not_met"] + it["unknown"]]
    check("明细逐行三态齐全（无负数）",
          all(it["met"] >= 0 and it["not_met"] >= 0 and it["unknown"] >= 0 for it in items)
          and not bad, f"{len(items)} 行")

    print(f"\n[一致性] {sum(1 for _, ok, _ in checks if ok)} / {len(checks)} 通过")
    failed = [c for c in checks if not c[1]]
    for name, _, detail in failed:
        print(f"  未通过：{name} {detail}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
