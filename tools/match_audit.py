"""资格匹配的逐条验收：**三份政策 × 三种画像**，每一条已复核规则都要有说得清的结果。

为什么不是"跑一次匹配看看有没有报错"：匹配是三态判定，一次成功只能证明接口通。
这里要证明的是**每一条规则都被正确求值**，所以做法是：

1. 从库里读出三份政策**已复核**（`review_status='approved'`）的资格 / 限制条件，
   作为"这次应该判几条"的唯一依据（不写死条数——复核状态变了，断言跟着变）；
2. 对每份政策各提交三个画像：
   * `eligible`：按政策条件明显满足（绩点高、排名前、已认定困难、无处分）
   * `ineligible`：明显不满足（绩点低、排名后、未认定、有处分）
   * `sparse`：只填学生类型，其余留空 → 必须落到 `unknown / missing_info`
3. 逐条断言：
   * 条目数 == 该文件已复核条件数（多一条少一条都算错）；
   * `status` 只能是三态；`unknown` 必须带 `unknown_reason`；
   * `not_met` 必须带 `rationale`（说清是哪个值不满足哪条要求）；
   * `met` 必须带政策依据（`evidence.quote` 或规则原文）；
   * `eligible` 画像下，**非 `manual_review` 的条件必须全部 `met`**；
   * `ineligible` 画像下，**每一条可比的条件必须 `not_met`**——这是"不把无法判断的情况
     强行标记为符合"的反面：也不能把明显不符合的判成符合；
   * `sparse` 画像下，**一条 `met` 都不许有**（信息不足时不许给结论）。
4. 结束前**只删本次创建的会话与条目**（按 session_id 精确删），不动任何既有数据。

用法：
    .venv\\Scripts\\python.exe tools/match_audit.py
    .venv\\Scripts\\python.exe tools/match_audit.py --keep     # 保留测试会话便于人工查看
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

ELIGIBLE = {
    "student_type": "full_time_undergraduate",
    "grade": 3,
    "gpa": 4.0,
    "rank_percent": 8,
    "major": "物联网工程",
    "credits_earned": 120,
    "enrollment_status": "enrolled",
    "poverty_certified": "yes",
    "disciplinary_record": "none",
}
INELIGIBLE = {
    "student_type": "full_time_undergraduate",
    "grade": 1,
    "gpa": 2.0,
    "rank_percent": 80,
    "major": "物联网工程",
    "credits_earned": 20,
    "enrollment_status": "enrolled",
    "poverty_certified": "no",
    "disciplinary_record": "any",
}
SPARSE = {
    "student_type": "full_time_undergraduate",
    # 其余八个字段一个字都不填
}

PROFILES = (("eligible", ELIGIBLE), ("ineligible", INELIGIBLE), ("sparse", SPARSE))


def api(base: str, path: str, body=None, token: str | None = None, method: str | None = None):
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method or ("POST" if body is not None else "GET"),
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
    ap.add_argument("--user", default="student")
    ap.add_argument("--password", default="student123")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    # 三份可匹配政策 = 至少有一条已复核资格/限制条件、且未删除
    docs = con.execute(
        """
        select d.id, d.title, count(r.id) n
        from documents d join policy_rules r on r.document_id = d.id
        where d.deleted_at is null
          and r.review_status = 'approved'
          and r.rule_type in ('eligibility', 'restriction')
        group by d.id, d.title
        having n > 0
        order by d.title
        """
    ).fetchall()

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))
        print(f"  {'OK  ' if ok else 'FAIL'} {name}{(' · ' + detail) if detail else ''}")

    token = api(args.base, "/auth/login", {"username": args.user, "password": args.password})["data"]["token"]
    created: list[str] = []

    print(f"[匹配验收] 可匹配政策 {len(docs)} 份，画像 3 种，共 {len(docs) * 3} 次真实匹配\n")
    try:
        for doc_id, title, approved_n in docs:
            print(f"== {title}（已复核条件 {approved_n} 条）")
            rules = {
                row[0]: row
                for row in con.execute(
                    "select rule_code, field_key, operator, expected_value "
                    "from policy_rules where document_id = ? and review_status='approved' "
                    "and rule_type in ('eligibility','restriction')", (doc_id,))
            }
            for label, profile in PROFILES:
                envelope = api(args.base, "/matches", {"document_id": doc_id, "profile": profile}, token)
                if envelope.get("code") != 0:
                    check(f"{label} 提交成功", False, f"code={envelope.get('code')} {envelope.get('message')}")
                    continue
                data = envelope["data"]
                created.append(data["session_id"])
                items = data["items"]
                statuses = [it["status"] for it in items]
                summary = data["summary"]

                check(f"{label}：条件条目数 = 已复核 {approved_n} 条",
                      len(items) == approved_n, f"实际 {len(items)} 条")
                check(f"{label}：三态取值合法",
                      set(statuses) <= {"met", "not_met", "unknown"}, f"{statuses}")
                check(f"{label}：summary 与实际计数一致",
                      (summary["met"], summary["not_met"], summary["unknown"])
                      == (statuses.count("met"), statuses.count("not_met"), statuses.count("unknown")),
                      f"summary {summary}")
                check(f"{label}：每条都有 rule_code 与理由字段完整",
                      all(it.get("rule_code") for it in items)
                      and all((it.get("rationale") or "").strip() for it in items),
                      f"{len(items)} 条")
                check(f"{label}：unknown 必须带 unknown_reason",
                      all(it.get("unknown_reason") for it in items if it["status"] == "unknown"),
                      str([(it["rule_code"], it["unknown_reason"]) for it in items if it["status"] == "unknown"]))
                check(f"{label}：met 必须能回溯到政策原文",
                      all((it.get("evidence") or {}).get("quote") or it.get("original_quote")
                          for it in items if it["status"] == "met"),
                      "")
                check(f"{label}：每条都带免责声明",
                      "不构成资格认定" in (data.get("disclaimer") or ""),
                      (data.get("disclaimer") or "")[:24])

                # ---- 画像相关的判定方向 ----
                by_code = {it["rule_code"]: it for it in items}
                manual = {code for code, r in rules.items() if r[2] == "manual_review"}

                if label == "eligible":
                    wrong = [c for c, it in by_code.items()
                             if c not in manual and it["status"] != "met"]
                    check("eligible：非主观条款全部判为符合", not wrong, f"未判符合：{wrong}")
                    check("eligible：至少一条符合（不是全 unknown 蒙过去）",
                          statuses.count("met") > 0, f"met={statuses.count('met')}")
                elif label == "ineligible":
                    # 明显不满足的画像：所有**可比**条件都该是 not_met。
                    # manual_review 不可比，只能是 unknown。
                    wrong = [c for c, it in by_code.items()
                             if c not in manual and it["status"] != "not_met"]
                    check("ineligible：可比条件全部判为不符合", not wrong, f"未判不符合：{wrong}")
                    check("ineligible：没有一条被判成符合",
                          statuses.count("met") == 0, f"met={statuses.count('met')}")
                    check("ineligible：unknown 只能是主观条款",
                          all(c in manual for c, it in by_code.items() if it["status"] == "unknown"),
                          str([c for c, it in by_code.items() if it["status"] == "unknown"]))
                else:
                    check("sparse：信息不足时一条结论都不给",
                          statuses.count("met") == 0 and statuses.count("not_met") == 0,
                          f"met={statuses.count('met')} not_met={statuses.count('not_met')}")
                    # 原因要"说得清"，但不要求全是同一种：
                    # 可比字段缺值 → missing_info；`manual_review` 这类主观条款
                    # 本来就判不了（"家庭经济困难"要看认定材料），它的正确原因是
                    # ambiguous_rule。要求全部 missing_info 是断言写窄了。
                    check("sparse：全部标 unknown，且可比字段的原因都是缺信息",
                          all(it["status"] == "unknown" for it in items)
                          and all(it["unknown_reason"] == "missing_info"
                                  for c, it in by_code.items() if c not in manual)
                          and all(it["unknown_reason"] in ("missing_info", "ambiguous_rule")
                                  for it in items),
                          str([(it["rule_code"], it["status"], it["unknown_reason"]) for it in items]))

                print("     " + " / ".join(f"{it['rule_code']}={it['status']}" for it in items))
    finally:
        if created and not args.keep:
            con.executemany("delete from match_result_items where session_id = ?",
                            [(sid,) for sid in created])
            con.executemany("delete from match_sessions where id = ?", [(sid,) for sid in created])
            con.commit()
            print(f"\n[清理] 已删除本次创建的 {len(created)} 条测试会话（只删这些 id）")
        elif created:
            print(f"\n[保留] {len(created)} 条测试会话留在库里：{created}")

    failed = [c for c in checks if not c[1]]
    print(f"\n[匹配验收] {len(checks) - len(failed)} / {len(checks)} 通过")
    for name, _, detail in failed:
        print(f"  未通过：{name} {detail}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
