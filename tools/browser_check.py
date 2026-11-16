"""界面巡检：用系统自带的 Edge（无头）真实打开页面，逐页断言并截图。

为什么必须有这一步：改完样式只跑 `vue-tsc` 与 `vite build`，只能证明"能编译"。
深色主题最容易出的问题是"某块还是浅底深字，读不出来"，而这类问题只有渲染后量出来。

断言四类：
1. **不该出现的字样**：里程碑 / M3–M7 / 接口 31 条 / 测试 222 项——产品界面不该有建设痕迹
2. **每页真有内容**：`#app` 的 innerText 长度（空壳/白屏会立刻掉下来）
3. **没有横向溢出**：`scrollWidth - innerWidth <= 2`
4. **JS 没有报错**：Runtime.exceptionThrown / console.error / 资源 404

用法：
    python tools/browser_check.py                 # 默认 http://127.0.0.1:5000
    python tools/browser_check.py http://127.0.0.1:5000 --out .workbuddy/shots
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(
    0, str(Path.home() / ".workbuddy" / "skills" / "headless-browser-verify-cdp" / "scripts")
)

from cdp import Cdp, launch  # noqa: E402

BANNED = ("里程碑", "阶段说明", "M3", "M4", "M5", "M6", "M7", "接口 31 条", "测试 222", "pytest")

PAGES = [
    ("工作台", "/", "dashboard"),
    ("智能问答", "/ask", "ask"),
    ("资格匹配", "/match", "match"),
    ("计划与任务", "/plans", "plans"),
    ("政策知识库", "/library", "library"),
]

# 管理员才有 /rules：学生访问会被路由挡回工作台（后端也 403）。
# 早先把它放进上面那张表，跑出来是"导航失败"——是测法错，不是 bug。
ADMIN_PAGES = [("规则管理", "/rules", "rules")]


def text_of(c: Cdp) -> str:
    return c.eval("document.body ? document.body.innerText : ''") or ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("base", nargs="?", default="http://127.0.0.1:5000")
    parser.add_argument("--out", default=".workbuddy/shots")
    parser.add_argument("--port", type=int, default=9333)
    args = parser.parse_args()

    base = args.base.rstrip("/")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    launch(port=args.port)
    c = Cdp(args.port)
    c.send("Emulation.setDeviceMetricsOverride",
           {"width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False})

    failures: list[str] = []
    notes: list[str] = []

    # 同一个 user-data-dir 会把上一轮的令牌留着，/login 会立刻跳回工作台。
    # 先落到同源页面清掉 localStorage，再验登录页。
    c.goto(f"{base}/")
    c.eval("localStorage.clear(); true")

    # ---------- 登录页 ----------
    c.goto(f"{base}/login")
    c.wait_for("!!document.querySelector('input[autocomplete=\"username\"]')", 20)
    login_text = text_of(c)
    for word in BANNED:
        if word in login_text:
            failures.append(f"登录页出现建设期字样「{word}」")
    # 登录页在 R11 之后改成了欢迎页 + 弹出的登录面板：根节点是 `.welcome`，
    # 原来的 `.login-page` 和"两个角色卡片（.role-chip）"都不存在了。
    # 脚本没跟着改，结果是每次都 `getComputedStyle(null)` 直接抛异常退出——
    # 看起来像"页面崩了"，其实是巡检脚本自己过期。判据改成：
    # 欢迎页在 + 用户名/密码两个输入框在（这才是登录页真正必需的东西）。
    fields = c.eval("""(() => ({
      user: !!document.querySelector('input[autocomplete="username"]'),
      pass: !!document.querySelector('input[autocomplete="current-password"]'),
      root: !!document.querySelector('.welcome'),
    }))()""")
    if not fields["root"]:
        failures.append("登录页根节点 .welcome 不存在")
    if not (fields["user"] and fields["pass"]):
        failures.append(f"登录表单缺字段：{fields}")
    bg = c.eval("""(() => {
      const el = document.querySelector('.welcome') || document.body;
      return getComputedStyle(el).backgroundColor;
    })()""")
    notes.append(f"登录页底色 {bg}")
    c.shot(str(out / "01-login.png"))
    print(f"[巡检] 登录页 ok · 输入框 user/pass={fields['user']}/{fields['pass']} · 底色 {bg}")

    # ---------- 以学生身份登录 ----------
    c.eval("""(() => {
      const u = document.querySelector('input[autocomplete="username"]');
      const p = document.querySelector('input[autocomplete="current-password"]');
      const set = (el, v) => {
        el.value = v;
        el.dispatchEvent(new Event('input', {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
      };
      set(u, 'student'); set(p, 'student123');
      document.querySelector('form .submit').click();
      return true;
    })()""")
    if not c.wait_for("location.pathname === '/' && !!document.querySelector('.side')", 25):
        failures.append("学生登录后没有进入工作台")
    c.clear_events() if hasattr(c, "clear_events") else None

    # ---------- 逐页巡检 ----------
    for idx, (label, path, slug) in enumerate(PAGES, start=2):
        c.goto(f"{base}{path}")
        time.sleep(1.0)
        ok = c.wait_for(f"location.pathname === '{path}'", 15)
        if not ok:
            failures.append(f"{label}：路由没到位（{c.eval('location.pathname')}）")
            continue

        # 列表页要真出数据。上一轮 7 页巡检全过、224 项测试全绿，
        # 但知识库页其实是「文件（0）」——因为断言只量了字数与溢出，
        # 没量表格行数。空表同样是"有内容的页面"，只有数行才测得出来。
        if path == "/library":
            rows = c.eval("document.querySelectorAll('.table tbody tr').length")
            heading = c.eval("(document.querySelector('.list-head h3')||{}).textContent || ''")
            if not rows:
                failures.append(f"{label}：文件列表 0 行（标题「{heading}」）")
            elif rows < 5:
                failures.append(f"{label}：文件列表只有 {rows} 行，像是没加载完")
            else:
                notes.append(f"知识库列表 {rows} 行 / 标题「{heading.strip()}」")

        body = text_of(c)
        if len(body) < 200:
            failures.append(f"{label}：页面内容过少（{len(body)} 字）")

        for word in BANNED:
            if word in body:
                failures.append(f"{label}：出现建设期字样「{word}」")

        metrics = c.eval("""(() => ({
          overflow: document.documentElement.scrollWidth - window.innerWidth,
          h: document.querySelector('.content') ? document.querySelector('.content').scrollHeight : 0,
          cards: document.querySelectorAll('.card').length,
        }))()""")
        if metrics["overflow"] > 2:
            failures.append(f"{label}：横向溢出 {metrics['overflow']}px")

        # 配色自检（R10 已换浅底，判据跟着反过来）：
        # 旧版这里断言"底色必须暗（bodyLum ≤ 0.6）"，那是 R10 之前深色主题的规矩；
        # 站点换成浅底青碧后这条一定失败——4 个页面全报"与深色主题不符"，
        # 于是真正的失败被这 4 条噪声盖住。现在改成：底色必须够浅（≥0.75），
        # 且正文必须明显比卡片深（文字亮度低于卡片 0.35 以上），才是"浅底深字"。
        contrast = c.eval("""(() => {
          const parse = (s) => (s.match(/\\d+/g) || []).slice(0, 3).map(Number);
          const lum = (rgb) => (0.2126*rgb[0] + 0.7152*rgb[1] + 0.0722*rgb[2]) / 255;
          const card = document.querySelector('.card');
          const body = document.body;
          const cs = getComputedStyle(card || body);
          return {
            bodyLum: +lum(parse(getComputedStyle(body).backgroundColor)).toFixed(2),
            cardLum: +lum(parse(cs.backgroundColor)).toFixed(2),
            textLum: +lum(parse(cs.color)).toFixed(2),
          };
        })()""")
        if contrast["bodyLum"] < 0.75:
            failures.append(f"{label}：页面底色偏暗（{contrast['bodyLum']}），与浅底主题不符")
        if contrast["cardLum"] - contrast["textLum"] < 0.35:
            failures.append(f"{label}：文字与卡片对比不足（card {contrast['cardLum']} / text {contrast['textLum']}）")

        c.shot(str(out / f"{idx:02d}-{slug}.png"))
        print(f"[巡检] {label:<6} ok · 文字 {len(body):>5} · 卡片 {metrics['cards']:>2} · "
              f"溢出 {metrics['overflow']} · 底/卡/字 {contrast['bodyLum']}/{contrast['cardLum']}/{contrast['textLum']}")

    # ---------- 换管理员，检查只有 admin 能进的页面 ----------
    c.eval("localStorage.clear(); true")
    c.goto(f"{base}/login")
    c.wait_for("!!document.querySelector('input[autocomplete=\"username\"]')", 20)
    c.eval("""(() => {
      const set = (el, v) => {
        el.value = v;
        el.dispatchEvent(new Event('input', {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
      };
      set(document.querySelector('input[autocomplete="username"]'), 'admin');
      set(document.querySelector('input[autocomplete="current-password"]'), 'admin123');
      document.querySelector('form .submit').click();
      return true;
    })()""")
    if not c.wait_for("location.pathname === '/' && !!document.querySelector('.side')", 25):
        failures.append("管理员登录后没有进入工作台")

    for idx, (label, path, slug) in enumerate(ADMIN_PAGES, start=len(PAGES) + 2):
        c.goto(f"{base}{path}")
        time.sleep(1.0)
        if not c.wait_for(f"location.pathname === '{path}'", 15):
            failures.append(f"{label}：管理员也被挡回（{c.eval('location.pathname')}）")
            continue
        body = text_of(c)
        if len(body) < 200:
            failures.append(f"{label}：页面内容过少（{len(body)} 字）")
        for word in BANNED:
            if word in body:
                failures.append(f"{label}：出现建设期字样「{word}」")
        metrics = c.eval("""(() => ({
          overflow: document.documentElement.scrollWidth - window.innerWidth,
          cards: document.querySelectorAll('.card').length,
        }))()""")
        if metrics["overflow"] > 2:
            failures.append(f"{label}：横向溢出 {metrics['overflow']}px")
        c.shot(str(out / f"{idx:02d}-{slug}.png"))
        print(f"[巡检] {label:<6} ok · 文字 {len(body):>5} · 卡片 {metrics['cards']:>2} · 溢出 {metrics['overflow']}")

    errors = c.errors() if hasattr(c, "errors") else []
    if errors:
        for err in errors[:6]:
            failures.append(f"JS 报错：{err}")

    c.close()

    print("\n[巡检] 截图目录", out.resolve())
    if notes:
        print("[巡检] 备注")
        for n in notes:
            print("   -", n)
    if failures:
        print("\n[巡检] 失败项：")
        for f in failures:
            print("  -", f)
        return 1
    print("\n[巡检] 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
