"""全系统功能回归（接口级）：学生端 10 项 + 管理员端 5 项，**写操作自带清理**。

这一层的定位：`pytest` 验的是函数与路由逻辑（临时库），浏览器脚本验的是渲染与交互，
中间还缺一层——**真实服务 + 真实库 + 真实角色**下"每个功能能不能跑通"。
所以这里逐个模块走一遍"读 → 写 → 再读 → 删"，每一步都断言。

写操作全部**只删自己刚创建的那几条**（按 id），不碰任何既有数据：

* 计划 / 任务 / 匹配会话：接口没有删除端点，收尾时按 id 从库里精确删；
* 日程 / 收藏：走各自的 DELETE 端点，删完还要断言"确实没了"。

用法：
    .venv\\Scripts\\python.exe tools\\regression_check.py
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PROFILE = {
    "student_type": "full_time_undergraduate",
    "grade": 3,
    "gpa": 4.0,
    "rank_percent": 8,
    "major": "物联网工程",
    "poverty_certified": "yes",
    "disciplinary_record": "none",
}


class Api:
    def __init__(self, base: str, token: str = ""):
        self.base = base.rstrip("/")
        self.token = token

    def __call__(self, method: str, path: str, body=None):
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={
                "Content-Type": "application/json",
                **({"Authorization": "Bearer " + self.token} if self.token else {}),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as err:
            try:
                return err.code, json.load(err)
            except Exception:
                return err.code, {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:5000/api/v1")
    ap.add_argument("--db", default=str(ROOT / "data" / "app.db"))
    args = ap.parse_args()

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))
        print(f"  {'OK  ' if ok else 'FAIL'} {name}{(' · ' + detail) if detail else ''}")

    anon = Api(args.base)
    status, payload = anon("POST", "/auth/login", {"username": "student", "password": "student123"})
    student = Api(args.base, payload["data"]["token"])
    _, admin_payload = anon("POST", "/auth/login", {"username": "admin", "password": "admin123"})
    admin = Api(args.base, admin_payload["data"]["token"])

    con = sqlite3.connect(args.db)
    created_sessions: list[str] = []
    created_plans: list[str] = []

    try:
        print("== 学生端")
        # 1 知识库
        status, data = student("GET", "/documents?page=1&page_size=100")
        docs = (data.get("data") or {}).get("items") or []
        check("知识库列表可用且不为空", status == 200 and len(docs) >= 18, f"{len(docs)} 份")

        # 2 匹配页政策
        status, data = student("GET", "/documents?match_ready=1")
        ready = (data.get("data") or {}).get("items") or []
        check("匹配页只列可匹配政策（3 份）", status == 200 and len(ready) == 3,
              f"{len(ready)} 份：" + "、".join(d["title"] for d in ready))

        # 3 事务指南
        status, data = student("GET", "/guides")
        check("事务指南概览可用", status == 200 and data.get("code") == 0, "")
        status, data = student("GET", "/guides/items")
        items = (data.get("data") or {}).get("items") or []
        check("事务指南条目列表非空", status == 200 and len(items) > 0, f"{len(items)} 条")
        if items:
            key = items[0].get("key") or items[0].get("id")
            status, data = student("GET", f"/guides/items/{key}")
            check("事务指南详情可打开", status == 200 and data.get("code") == 0, str(key))

        # 4 推荐 / 政策更新
        status, data = student("GET", "/recommendations")
        check("智能推荐可用", status == 200 and data.get("code") == 0, "")
        status, data = student("GET", "/policies/updates")
        check("政策更新可用", status == 200 and data.get("code") == 0, "")

        # 5 资格匹配：写 → 读回
        doc_id = ready[0]["id"]
        status, data = student("POST", "/matches", {"document_id": doc_id, "profile": PROFILE})
        ok = status == 201 or (status == 200 and data.get("code") == 0)
        check("资格匹配可提交", ok, f"HTTP {status} / code {data.get('code')}")
        session_id = (data.get("data") or {}).get("session_id") if ok else None
        if session_id:
            created_sessions.append(session_id)
            _, again = student("GET", f"/matches/{session_id}")
            check("匹配结果可按 id 读回",
                  again.get("code") == 0 and again["data"]["session_id"] == session_id, session_id)

        # 6 学习计划：草稿 → 确认 → 列表
        plan_doc = next((d for d in ready if "2026" in d["title"]), ready[0])
        status, data = student("POST", "/plans/draft",
                               {"document_id": plan_doc["id"], "match_session_id": session_id})
        draft = data.get("data") or {}
        plan_id = draft.get("plan_id")
        if plan_id:
            created_plans.append(plan_id)
            # draft 自己也会落一条匹配会话，一并记账
            if draft.get("match_session_id"):
                created_sessions.append(draft["match_session_id"])
        check("事务规划可生成草稿", bool(plan_id) and bool(draft.get("tasks")),
              f"plan {plan_id} / {len(draft.get('tasks') or [])} 条任务")
        if plan_id:
            body = {"tasks": draft.get("tasks") or []}
            status, data = student("POST", f"/plans/{plan_id}/confirm", body)
            check("草稿可确认成正式计划", status == 200 and data.get("code") == 0,
                  f"HTTP {status} / code {data.get('code')}")
            _, data = student("GET", "/plans")
            plans = (data.get("data") or {}).get("items") or []
            check("确认后的计划出现在列表里",
                  any(p["id"] == plan_id for p in plans), f"{len(plans)} 个计划")

        # 7 校园日程：增 → 查 → 删 → 确认已删
        event_body = {"title": "R24 回归测试日程", "date": "2026-12-01", "time": "09:30",
                      "category": "personal"}
        status, data = student("POST", "/calendar/events", event_body)
        event_id = (data.get("data") or {}).get("id")
        check("日程可新增", status in (200, 201) and bool(event_id), f"HTTP {status}")
        if event_id:
            # `GET /calendar` 默认只返回**当月**（`_month_span(今天)`），
            # 而这条测试日程落在 12 月。不带区间去查，永远查不到——不是日程没存进去。
            window = "/calendar?from=2026-12-01&to=2026-12-31"
            _, data = student("GET", window)
            blob = json.dumps(data, ensure_ascii=False)
            check("新增的日程出现在日历里", event_id in blob, event_id)
            status, _ = student("DELETE", f"/calendar/events/{event_id}")
            check("日程可删除", status in (200, 204), f"HTTP {status}")
            _, data = student("GET", window)
            check("删除后日历里确实没有了", event_id not in json.dumps(data, ensure_ascii=False), event_id)

        # 8 收藏：增 → 查 → 删
        status, data = student("POST", "/favorites", {"target_type": "document", "target_id": doc_id})
        check("收藏可新增", status in (200, 201) and data.get("code") == 0, f"HTTP {status}")
        _, data = student("GET", "/favorites/ids")
        ids = json.dumps(data, ensure_ascii=False)
        check("新增的收藏出现在收藏 id 列表里", doc_id in ids, "")
        status, _ = student("DELETE", f"/favorites/document/{doc_id}")
        check("收藏可取消", status in (200, 204), f"HTTP {status}")
        _, data = student("GET", "/favorites/ids")
        check("取消后收藏 id 列表里没有了", doc_id not in json.dumps(data, ensure_ascii=False), "")

        # 9 会话列表 / 任务
        status, data = student("GET", "/conversations")
        check("我的提问会话列表可用", status == 200 and data.get("code") == 0, "")
        status, data = student("GET", "/tasks")
        check("任务列表可用", status == 200 and data.get("code") == 0, "")
        status, data = student("GET", "/tasks/stats")
        check("任务统计可用", status == 200 and data.get("code") == 0, "")

        print("\n== 管理员端")
        status, data = admin("GET", "/admin/feedback")
        check("反馈列表可用", status == 200 and data.get("code") == 0, "")
        status, data = admin("GET", f"/documents/{doc_id}/rules")
        rules = (data.get("data") or {}).get("items") or []
        check("政策条件列表可用", status == 200 and len(rules) > 0, f"{len(rules)} 条")
        status, data = admin("GET", f"/documents/{doc_id}/chunks")
        chunks = (data.get("data") or {}).get("items") or []
        check("政策片段列表可用", status == 200 and len(chunks) > 0, f"{len(chunks)} 片")
        # 这条接口是**上传预检**：按标题回答"有没有同主题的既有文件"，
        # 所以 `title` 是必填的（缺了回 1001），不是"列全部候选"。
        status, data = admin(
            "GET", "/documents/version-candidates?title="
                   + urllib.parse.quote("本科生奖学金评审办法"))
        cands = (data.get("data") or {}).get("candidates") or []
        check("版本候选可用（按标题预检）", status == 200 and data.get("code") == 0,
              f"{len(cands)} 个候选")
        status, data = admin("GET", "/admin/analytics/portraits")
        check("数据中心·画像统计可用", status == 200 and data.get("code") == 0, "")
        status, data = admin("GET", "/admin/analytics/qa")
        check("数据中心·问答统计可用", status == 200 and data.get("code") == 0, "")
    finally:
        # 只删本次创建的行；计划要先删任务（外键）再删计划
        for plan_id in created_plans:
            con.execute("delete from tasks where plan_id = ?", (plan_id,))
            con.execute("delete from action_plans where id = ?", (plan_id,))
        for sid in created_sessions:
            con.execute("delete from match_result_items where session_id = ?", (sid,))
            con.execute("delete from match_sessions where id = ?", (sid,))
        con.commit()
        if created_plans or created_sessions:
            print(f"\n[清理] 删除本次创建的 {len(created_plans)} 个计划 / {len(created_sessions)} 条匹配会话")

    failed = [c for c in checks if not c[1]]
    print(f"\n[回归] {len(checks) - len(failed)} / {len(checks)} 通过")
    for name, _, detail in failed:
        print(f"  未通过：{name} {detail}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
