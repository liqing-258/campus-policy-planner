"""权限验收：**该拦的拦住，该放的放行**，两条都要有证据。

只验"学生打不开管理端"是不够的——把所有接口都拦掉也一样能通过。所以这里每个角色
各跑一组：学生撞管理接口必须 403，管理员打同一批接口必须 200。任何一边不过都算失败。

覆盖面：

1. 未登录 / 伪造令牌 → 401（不是 403：这是"你是谁不知道"，不是"你没权限"）；
2. 学生 → 管理接口 6 条（数据中心两个、反馈统计、上传、改文件、删文件）→ 403；
3. 管理员 → 同一批（读接口）→ 200；
4. 会话隔离：学生读**别人的**会话 → 403/404，读自己的 → 200；
5. 记录"登出"的真实语义：本项目 JWT 无状态、服务端无吊销表，
   退出登录只清前端令牌，旧令牌在有效期内仍可用（**已知限制，不是本轮引入**）。

用法：
    .venv\\Scripts\\python.exe tools/permission_check.py
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def probe(base: str, method: str, path: str, token: str | None = None, body=None):
    """返回 (HTTP 状态码, 业务码, 消息)。4xx 不抛异常，交给断言判断。"""
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Content-Type": "application/json",
            **({"Authorization": "Bearer " + token} if token else {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.load(resp)
            return resp.status, payload.get("code"), payload.get("message")
    except urllib.error.HTTPError as err:
        try:
            payload = json.load(err)
        except Exception:
            payload = {}
        return err.code, payload.get("code"), payload.get("message")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:5000/api/v1")
    ap.add_argument("--db", default=str(ROOT / "data" / "app.db"))
    args = ap.parse_args()
    base = args.base

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))
        print(f"  {'OK  ' if ok else 'FAIL'} {name}{(' · ' + detail) if detail else ''}")

    def login(user: str, pwd: str) -> str:
        status, code, _ = probe(base, "POST", "/auth/login", body={"username": user, "password": pwd})
        if status != 200 or code != 0:
            raise SystemExit(f"登录失败 {user}: {status}/{code}")
        _, _, _ = status, code, _
        req = urllib.request.Request(
            base + "/auth/login",
            data=json.dumps({"username": user, "password": pwd}).encode(),
            method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)["data"]["token"]

    student = login("student", "student123")
    admin = login("admin", "admin123")

    con = sqlite3.connect(args.db)
    ids = dict(con.execute("select username, id from users").fetchall())
    admin_conv = con.execute(
        "select id from conversations where user_id = ? limit 1", (ids["admin"],)).fetchone()
    student_conv = con.execute(
        "select id from conversations where user_id = ? limit 1", (ids["student"],)).fetchone()
    a_rule = con.execute(
        "select id from policy_rules order by created_at limit 1").fetchone()
    a_doc = con.execute(
        "select id from documents where deleted_at is null limit 1").fetchone()

    # ---------- 1 未认证 ----------
    status, code, _ = probe(base, "GET", "/admin/analytics/portraits")
    check("未带令牌访问管理接口 → 401", status == 401, f"HTTP {status} / code {code}")
    status, code, _ = probe(base, "GET", "/admin/analytics/portraits", token="not-a-real-token")
    check("伪造令牌 → 401", status == 401, f"HTTP {status} / code {code}")
    status, code, _ = probe(base, "GET", "/auth/me", token="a.b.c")
    check("伪造令牌访问 /auth/me → 401", status == 401, f"HTTP {status} / code {code}")

    # ---------- 2 学生越权 ----------
    admin_only = [
        ("GET", "/admin/analytics/portraits", "数据中心·画像统计"),
        ("GET", "/admin/analytics/qa", "数据中心·问答统计"),
        ("GET", "/admin/feedback/stats", "管理员·反馈统计"),
        ("POST", "/documents", "上传政策文件"),
        ("PATCH", f"/documents/{a_doc[0]}" if a_doc else "/documents/x", "修改政策文件"),
        ("DELETE", f"/documents/{a_doc[0]}" if a_doc else "/documents/x", "删除政策文件"),
        ("PATCH", f"/rules/{a_rule[0]}" if a_rule else "/rules/x", "复核政策条件"),
    ]
    for method, path, label in admin_only:
        status, code, msg = probe(base, method, path, token=student, body={} if method != "GET" else None)
        check(f"学生不能「{label}」", status == 403, f"HTTP {status} / code {code} / {msg}")

    # ---------- 3 管理员放行 ----------
    for path, label in (
        ("/admin/analytics/portraits", "数据中心·画像统计"),
        ("/admin/analytics/qa", "数据中心·问答统计"),
        ("/admin/feedback/stats", "管理员·反馈统计"),
    ):
        status, code, msg = probe(base, "GET", path, token=admin)
        check(f"管理员可以「{label}」", status == 200 and code == 0, f"HTTP {status} / code {code}")

    # 学生自己的常规功能必须能用（否则"全拦住"也能过上面的断言）
    status, code, _ = probe(base, "GET", "/documents?page=1&page_size=5", token=student)
    check("学生可以查知识库列表", status == 200 and code == 0, f"HTTP {status}")
    status, code, _ = probe(base, "GET", "/documents?match_ready=1", token=student)
    check("学生可以查可匹配政策（match_ready）", status == 200 and code == 0, f"HTTP {status}")

    # ---------- 4 会话隔离 ----------
    if admin_conv:
        status, code, _ = probe(base, "GET", f"/conversations/{admin_conv[0]}/messages", token=student)
        check("学生读不到管理员的会话", status in (403, 404), f"HTTP {status} / code {code}")
    if student_conv:
        status, code, _ = probe(base, "GET", f"/conversations/{student_conv[0]}/messages", token=student)
        check("学生读得到自己的会话", status == 200 and code == 0, f"HTTP {status}")

    # ---------- 5 登出的真实语义 ----------
    # 本项目没有 /auth/logout 端点，JWT 无状态、服务端无吊销表：
    # 退出登录 = 前端丢掉令牌。旧令牌在有效期内（JWT_EXPIRE_MINUTES）仍然可用。
    status, code, _ = probe(base, "GET", "/auth/me", token=student)
    check("登出后旧令牌仍可用（已知限制，如实登记）",
          status == 200,
          f"HTTP {status}——服务端无吊销表，刷新前旧令牌有效；不影响演示，风险已登记")

    failed = [c for c in checks if not c[1]]
    print(f"\n[权限] {len(checks) - len(failed)} / {len(checks)} 通过")
    for name, _, detail in failed:
        print(f"  未通过：{name} {detail}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
