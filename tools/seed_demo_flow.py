"""把演示库跑出一条真实的「匹配 → 计划 → 任务」链路，并留下几种不同状态的任务。

为什么需要它：M6 的工作台新增「任务分布」卡，但演示库原本 `plans=0 / tasks=0`，
五根柱子全是 0 —— 单测全绿、界面却是空的，正是 M6 里被记下来的那类缺陷
（`08-M6体验与质量.md` §L.4 第 9 条：单测绿 ≠ 功能可用）。

它**只走真实接口**（不打桩、不写 SQL）：student 登录 → /matches → /plans/draft →
/plans/{id}/confirm → /tasks/{id}/status。因此跑通它本身就证明链路是活的。

用法（需要服务已启动）：
    python tools/seed_demo_flow.py                  # 默认 http://127.0.0.1:5000
    python tools/seed_demo_flow.py --base http://127.0.0.1:5055
    python tools/seed_demo_flow.py --dry-run        # 只跑到草稿，不确认落库

退出码：0 成功；1 任一环节失败（并打印业务码与消息）。
"""

from __future__ import annotations

import argparse
import json
import sys

import httpx

STUDENT = {"username": "student", "password": "student123"}

# 一份能被判"符合"的画像：绩点 3.2 在励志奖学金（gpa>=3.0）之上、在优秀奖学金（gpa>=3.5）之下。
# 字段名与取值都必须是白名单里的（shared/enums.py:PROFILE_FIELDS），
# 且 `poverty_certified` / `disciplinary_record` 的取值是 yes / none —— 规则里就是这么写的。
PROFILE = {
    "student_type": "全日制在校本科生（具有本校正式学籍）",
    "grade": 3,
    "gpa": 3.2,
    "rank_percent": 12,
    "poverty_certified": "yes",
    "disciplinary_record": "none",
    "major": "物联网工程",
    "credits_earned": 120,
    "enrollment_status": "在读",
}

# 不传奖项时，优秀奖学金那条 gpa>=3.5 会把 3.2 判成不符合；传了只比对应奖项的规则
TARGET_GROUP = "国家励志奖学金申请人"

# 确认后依次改成的状态：做出「有的在做、有的做完、有的延后、有的作废」的分布
STATUS_PLAN = ["done", "doing", "deferred", "cancelled"]


def api(client: httpx.Client, base: str, method: str, path: str, token: str | None = None, **kw):
    headers = kw.pop("headers", {})
    if token:
        headers["Authorization"] = "Bearer " + token
    resp = client.request(method, base + path, headers=headers, **kw)
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        print(f"  非 JSON 响应 {resp.status_code}: {resp.text[:200]}")
        sys.exit(1)
    return body


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:5000")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    base = args.base.rstrip("/") + "/api/v1"

    # trust_env=False：本机代理会劫持 127.0.0.1（M5 踩过，502 upstream connect failed）
    client = httpx.Client(timeout=180.0, trust_env=False)

    body = api(client, base, "POST", "/auth/login", json=STUDENT)
    if body.get("code") != 0:
        print("登录失败:", body)
        return 1
    token = body["data"]["token"]
    print("[1] student 登录 ok")

    body = api(client, base, "GET", "/documents?page=1&page_size=20", token)
    docs = body["data"]["items"] if isinstance(body["data"], dict) else body["data"]
    # 取"现行有效"的那一版：2026-2027 学年的奖学金办法
    target = None
    for d in docs:
        if "奖学金" in d["title"] and (d.get("academic_year") or "").startswith("2026"):
            target = d
            break
    if target is None:
        print("找不到 2026-2027 的奖学金办法，先跑 tools/seed_demo.py 与 seed_demo_versions.py")
        return 1
    print(f"[2] 目标文档 {target['id']}  {target['title'][:40]}")

    body = api(client, base, "POST", "/matches", token,
               json={"document_id": target["id"], "profile": PROFILE,
                     "target_group": TARGET_GROUP})
    if body.get("code") != 0:
        print("匹配失败:", body.get("code"), body.get("message"))
        return 1
    data = body["data"]
    session_id = data.get("session_id") or data.get("id")
    summary = data.get("summary") or {}
    print(f"[3] 匹配 ok  session={session_id}  summary={json.dumps(summary, ensure_ascii=False)}")

    body = api(client, base, "POST", "/plans/draft", token,
               json={"document_id": target["id"], "match_session_id": session_id})
    if body.get("code") != 0:
        print("草稿失败:", body.get("code"), body.get("message"))
        return 1
    draft = body["data"]
    plan_id = draft.get("plan_id") or draft.get("id")
    tasks = draft.get("tasks") or []
    print(f"[4] 草稿 ok  plan={plan_id}  任务 {len(tasks)} 条")

    if args.dry_run:
        print("\n--dry-run：不确认落库。草稿任务如下")
        print(json.dumps(tasks, ensure_ascii=False, indent=2)[:1200])
        return 0

    # 草稿任务字段与 DraftTaskInput 同名，原样回传即可（不要自己造 temp_id）
    body = api(client, base, "POST", f"/plans/{plan_id}/confirm", token,
               json={"title": draft.get("title"), "tasks": tasks})
    if body.get("code") != 0:
        print("确认失败:", body.get("code"), body.get("message"))
        return 1
    plan = body["data"]
    print(f"[5] 确认 ok  计划状态={plan.get('status')}")

    body = api(client, base, "GET", f"/plans?page=1&page_size=50", token)
    items = body["data"]["items"] if isinstance(body["data"], dict) else body["data"]
    real_plan = next((p for p in items if p["id"] == plan_id), None)
    title = (real_plan or {}).get("title") or "演示计划"

    body = api(client, base, "GET", f"/tasks?plan_id={plan_id}&page=1&page_size=50", token)
    tdata = body["data"]
    task_list = tdata["items"] if isinstance(tdata, dict) else tdata
    print(f"[6] 任务落库 {len(task_list)} 条")

    # 按计划改状态，制造分布；状态机不允许的跳转会返回 409，这里如实打印而不是忽略
    for i, task in enumerate(task_list[: len(STATUS_PLAN)]):
        want = STATUS_PLAN[i]
        body = api(client, base, "POST", f"/tasks/{task['id']}/status", token,
                   json={"status": want})
        if body.get("code") == 0:
            print(f"    {task['title'][:24]:<26} -> {want}")
        else:
            print(f"    {task['title'][:24]:<26} -> {want} 失败: {body.get('code')} {body.get('message')}")

    body = api(client, base, "GET", "/tasks/stats", token)
    st = body["data"]
    print(f"[7] 任务统计 total={st.get('total')} 完成率={st.get('completion_rate')}% "
          f"逾期={st.get('overdue')} 即将到期={st.get('due_soon')}")
    print("    分布:", json.dumps(st.get("by_status"), ensure_ascii=False))
    print(f"\n完成：计划「{title}」下 {st.get('total')} 条任务，状态已铺开。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
