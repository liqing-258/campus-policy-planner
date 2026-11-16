"""登录入口验收（R23）：落地页恒显 + 「开启系统」的两条进入路径。

被测的是**进入路径**，不是登录接口本身（接口有 pytest 覆盖）。
R23 之前的行为是：本地还留着令牌 → 落地页 `router.replace('/')` 自己弹走，
于是"登录网址"永远看不到首屏，双击网址直接落在工作台。

判据全部是**渲染后的事实**，不是源码：

1. 无 token 访问 `/login` → 落地页（系统名 / 一句话 / 「开启系统」/ 知识库规模），
   **URL 不被弹走**、没有登录框、没有遮罩；
2. 点「开启系统」→ 登录小窗（用户名/密码输入框）；Esc 可关；
3. 登录成功 → 工作台；
4. **带 token 再访问 `/login` → 仍是落地页**（R23 的核心回归点），
   且显示"换个账号登录"入口；
5. 带 token 点「开启系统」→ **直接进工作台**（不弹登录框）；
6. `/login?open=1` → 直接拉开登录小窗（验收脚本与深链用）；
7. "换个账号登录"→ 拉开登录小窗；
8. 全程无横向溢出、无 JS 异常。

用法：
    .venv/Scripts/python.exe tools/login_flow_check.py            # 默认 http://127.0.0.1:5000
    .venv/Scripts/python.exe tools/login_flow_check.py --out .workbuddy/shots/r23
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(
    0, str(Path.home() / ".workbuddy" / "skills" / "headless-browser-verify-cdp" / "scripts")
)
from cdp import Cdp, launch  # noqa: E402


def set_field(c: Cdp, selector: str, value: str) -> None:
    c.eval(
        """(([sel, v]) => {
          const el = document.querySelector(sel);
          el.value = v;
          el.dispatchEvent(new Event('input', {bubbles: true}));
          el.dispatchEvent(new Event('change', {bubbles: true}));
        })(%s)"""
        % repr([selector, value])
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("base", nargs="?", default="http://127.0.0.1:5000")
    parser.add_argument("--out", default=".workbuddy/shots/r23")
    parser.add_argument("--port", type=int, default=9340)
    args = parser.parse_args()

    base = args.base.rstrip("/")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    launch(port=args.port)
    c = Cdp(args.port)
    c.send("Emulation.setDeviceMetricsOverride",
           {"width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False})
    fails: list[str] = []
    notes: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            fails.append(msg)

    # ---------- 1. 无 token：落地页，不被弹走 ----------
    c.goto(f"{base}/")
    c.eval("localStorage.clear(); true")
    c.goto(f"{base}/login")
    time.sleep(0.8)
    st = c.eval("""(() => ({
      path: location.pathname + location.search,
      hero: !!document.querySelector('.welcome'),
      start: !!document.querySelector('.start'),
      startText: (document.querySelector('.start') || {}).innerText || '',
      brand: (document.querySelector('.brand-row .title') || {}).innerText || '',
      tagline: !!document.querySelector('.tagline'),
      scale: (document.querySelector('.scale') || {}).innerText || '',
      mask: !!document.querySelector('.mask'),
      userField: !!document.querySelector('input[autocomplete="username"]'),
      side: !!document.querySelector('.side'),
    }))()""")
    check(st["hero"], "未登录访问 /login 没有落地页")
    check(st["start"] and "开启系统" in st["startText"], f"「开启系统」按钮缺失：{st['startText']!r}")
    check("知" in st["brand"] and "校" in st["brand"], f"首屏系统名不对：{st['brand']!r}")
    check(st["tagline"], "首屏一句话缺失")
    check("知识库已收录" in st["scale"], f"知识库规模行缺失：{st['scale']!r}")
    check(not st["mask"], "落地页不该自带遮罩（登录窗应是关着的）")
    check(not st["userField"], "落地页不该出现登录输入框（R23：默认不开窗）")
    check(not st["side"], "落地页不该套应用外壳（侧栏）")
    check(st["path"].startswith("/login"), f"/login 被弹走了：{st['path']}")
    notes.append(f"未登录：落地页 ok · 规模「{st['scale'][:40]}」· URL {st['path']}")
    c.shot(str(out / "01-login-landing.png"))

    # ---------- 2. 开启系统 → 登录窗；Esc 可关 ----------
    c.eval("document.querySelector('.start').click(); true")
    opened = c.wait_for("!!document.querySelector('input[autocomplete=\"username\"]')", 6)
    check(opened, "点「开启系统」没拉开登录小窗")
    check(bool(c.eval("!!document.querySelector('.mask')")), "登录小窗没有遮罩层")
    c.shot(str(out / "02-login-panel.png"))
    c.send("Input.dispatchKeyEvent",
           {"type": "keyDown", "key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27})
    c.send("Input.dispatchKeyEvent",
           {"type": "keyUp", "key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27})
    closed = c.wait_for("!document.querySelector('.mask')", 6)
    check(closed, "Esc 关不掉登录小窗")
    check(bool(c.eval("!!document.querySelector('.start')")), "关窗后落地页不在了")
    notes.append("开启系统 → 登录窗 → Esc 关闭 ok")

    # ---------- 3. 正常登录一次（admin）→ 工作台 ----------
    c.eval("document.querySelector('.start').click(); true")
    c.wait_for("!!document.querySelector('input[autocomplete=\"username\"]')", 6)
    set_field(c, 'input[autocomplete="username"]', "admin")
    set_field(c, 'input[autocomplete="current-password"]', "admin123")
    c.eval("document.querySelector('form .submit').click(); true")
    entered = c.wait_for("location.pathname === '/' && !!document.querySelector('.side')", 25)
    check(entered, "登录后没有进入工作台")
    notes.append("登录 admin → 工作台 ok")

    # ---------- 4. 核心回归点：带 token 再访问 /login 仍见落地页 ----------
    c.goto(f"{base}/login")
    time.sleep(0.8)
    st2 = c.eval("""(() => ({
      path: location.pathname,
      hero: !!document.querySelector('.welcome'),
      mask: !!document.querySelector('.mask'),
      userField: !!document.querySelector('input[autocomplete="username"]'),
      signed: (document.querySelector('.signed') || {}).innerText || '',
      side: !!document.querySelector('.side'),
    }))()""")
    check(st2["hero"], f"带 token 访问 /login 被弹走了（{st2['path']}）——R23 核心回归点")
    check(not st2["mask"], "带 token 访问 /login 不该自动开登录窗")
    check(not st2["userField"], "带 token 访问 /login 不该出现登录输入框")
    check("换个账号" in st2["signed"], f"缺换账号入口：{st2['signed']!r}")
    check(not st2["side"], "带 token 访问 /login 不该套应用外壳")
    notes.append(f"带 token：仍见落地页 ok · 换账号入口「{st2['signed'].strip()}」")
    c.shot(str(out / "03-login-landing-authed.png"))

    # ---------- 5. 带 token 点「开启系统」→ 直接进系统（不弹窗） ----------
    c.eval("document.querySelector('.start').click(); true")
    fast = c.wait_for("location.pathname === '/' && !!document.querySelector('.side')", 8)
    check(fast, "带 token 点「开启系统」没有进入系统")
    check(not bool(c.eval("!!document.querySelector('.mask')")),
          "带 token 点「开启系统」不该弹登录窗（令牌还有效）")
    notes.append("带 token 点开启系统 → 直接进工作台 ok")

    # ---------- 6. ?open=1 → 直接开窗 ----------
    c.goto(f"{base}/login?open=1")
    ok_open = c.wait_for("!!document.querySelector('input[autocomplete=\"username\"]')", 8)
    check(ok_open, "/login?open=1 没有直接拉开登录小窗")
    notes.append("/login?open=1 直接开窗 ok")

    # ---------- 7. 换个账号入口 ----------
    c.goto(f"{base}/login")
    time.sleep(0.6)
    c.eval("""(() => {
      const btns = [...document.querySelectorAll('.signed .link')];
      if (btns[0]) btns[0].click();
      return true;
    })()""")
    swap = c.wait_for("!!document.querySelector('input[autocomplete=\"username\"]')", 6)
    check(swap, "「换个账号登录」没有拉开登录小窗")
    notes.append("换账号入口 → 登录窗 ok")

    # ---------- 8. 溢出与 JS 异常 ----------
    overflow = c.eval("document.documentElement.scrollWidth - window.innerWidth")
    check(overflow <= 2, f"落地页横向溢出 {overflow}px")
    # 滤掉 401 的 console 噪声：浏览器把 `GET /auth/me` 的 401 也记成一条 LOG。
    # 那是**预期语义**——令牌过期/无效时后端回 401，前端 restore() 据此当未登录，
    # 不是 JS 异常。真异常（Runtime.exceptionThrown / console.error / 404 资源）照旧判失败。
    errs = [e for e in (c.errors() if hasattr(c, "errors") else [])
            if "401" not in e or "UNAUTHORIZED" not in e]
    check(not errs, f"JS 异常 {len(errs)} 条：{errs[:3]}")

    print("\n===== 观测值 =====")
    for n in notes:
        print(" ·", n)
    print("\n===== 断言 =====")
    if fails:
        for f in fails:
            print(" FAIL", f)
        print(f"\n结果：{len(fails)} 项不通过")
        return 1
    print(" 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
