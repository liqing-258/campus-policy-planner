"""问答页交互巡检：把"布局要求"变成可执行的断言。

`browser_check.py` 只验"每页有内容、不溢出、没有建设期字样"，它看不见
"输入框是不是真在底部""建议问题是不是只在聚焦时出现"这类交互约束。
所以这一轮的五条界面要求，逐条落成下面这些断言——**测不了就等于没做**：

1. 输入框固定在页面底部，且与左侧解答区同宽对齐
2. 未提问时：聚焦输入框才出现三条「你可以这样问」；失焦即消失
3. 提问后：解答在上方主区，标题是「解答」而不是「答案」
4. 提问后：输入框上方常驻三条「继续追问」
5. 右上角「最近提问」可折叠右侧历史；点历史只填入输入框、不自动提交
6. 点历史后再提问，续在原会话里——不在右侧历史里多出一行同题记录
7. 引用给出站内原文链接，点进去落在知识库对应文件上（行高亮 + 片段展开）

用法：
    python tools/ask_flow_check.py --out .workbuddy/shots/askflow
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

QUESTION = "申请国家励志奖学金需要满足哪些条件？"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("base", nargs="?", default="http://127.0.0.1:5000")
    parser.add_argument("--out", default=".workbuddy/shots/askflow")
    parser.add_argument("--port", type=int, default=9334)
    args = parser.parse_args()

    base = args.base.rstrip("/")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    launch(port=args.port)
    c = Cdp(args.port)
    c.send("Emulation.setDeviceMetricsOverride",
           {"width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False})
    # 无头窗口没有系统焦点时，`element.focus()` 会设置 activeElement 但**不派发 focus 事件**，
    # 于是"聚焦才显示建议"这类用例会假失败。打开焦点模拟后 document.hasFocus() 为 true。
    c.send("Emulation.setFocusEmulationEnabled", {"enabled": True})

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))
        print(f"  {'OK  ' if ok else 'FAIL'} {name}{(' · ' + detail) if detail else ''}")

    def text() -> str:
        return c.eval("document.body ? document.body.innerText : ''") or ""

    # ---------- 登录 ----------
    c.goto(f"{base}/")
    c.eval("localStorage.clear(); true")
    c.goto(f"{base}/login")
    c.wait_for("!!document.querySelector('input[autocomplete=\"username\"]')", 20)
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
    c.wait_for("location.pathname === '/' && !!document.querySelector('.side')", 25)

    c.goto(f"{base}/ask")
    c.wait_for("!!document.querySelector('.composer-box textarea')", 20)
    time.sleep(0.6)

    # ---------- 1. 输入框在页面底部 ----------
    geo = c.eval("""(() => {
      const box = document.querySelector('.composer-box').getBoundingClientRect();
      const bar = document.querySelector('.composer').getBoundingClientRect();
      const main = document.querySelector('.chat-main').getBoundingClientRect();
      const side = document.querySelector('.chat-side');
      return {
        gapBottom: Math.round(window.innerHeight - bar.bottom),
        boxLeft: Math.round(box.left), boxRight: Math.round(box.right),
        mainLeft: Math.round(main.left), mainRight: Math.round(main.right),
        sideVisible: side ? getComputedStyle(side).display !== 'none' : false,
        sideWidth: side ? Math.round(side.getBoundingClientRect().width) : 0,
      };
    })()""")
    check("输入区贴在视口底部", abs(geo["gapBottom"]) <= 12, f"距底 {geo['gapBottom']}px")
    check("输入框与左侧解答区左右对齐",
          abs(geo["boxLeft"] - geo["mainLeft"]) <= 2 and abs(geo["boxRight"] - geo["mainRight"]) <= 2,
          f"输入框 {geo['boxLeft']}–{geo['boxRight']} / 主区 {geo['mainLeft']}–{geo['mainRight']}")
    check("右侧历史栏默认展开", geo["sideVisible"], f"宽 {geo['sideWidth']}px")
    check("欢迎区没有一上来就铺建议", "你可以这样问" not in text())
    c.shot(str(out / "01-ask-idle.png"))

    # ---------- 2. 聚焦才出现三条建议 ----------
    c.eval("document.querySelector('.composer-box textarea').focus(); true")
    time.sleep(0.4)
    chips = c.eval("Array.from(document.querySelectorAll('.suggest .chip')).map(e => e.innerText)")
    check("聚焦后出现「你可以这样问」", "你可以这样问" in text())
    check("建议问题恰好三条", len(chips or []) == 3, " / ".join(chips or []))
    c.shot(str(out / "02-ask-focus.png"))

    c.eval("document.querySelector('.composer-box textarea').blur(); true")
    time.sleep(0.3)
    check("失焦后建议收回", "你可以这样问" not in text())

    # ---------- 3. 提问 → 解答在上方 ----------
    c.eval(f"""(() => {{
      const el = document.querySelector('.composer-box textarea');
      el.value = {QUESTION!r};
      el.dispatchEvent(new Event('input', {{bubbles: true}}));
      return true;
    }})()""")
    c.eval("document.querySelector('.send').click(); true")
    if not c.wait_for("!!document.querySelector('.answer-text')", 120):
        check("提问后出现解答", False, "120 秒内没有 .answer-text")
        for name, ok, detail in checks:
            if not ok:
                print(f"未通过：{name} {detail}")
        return 1

    time.sleep(0.5)
    body = text()
    check("解答区标题是「解答」", "解答" in body and "答案" not in body)
    check("正文里没有「答案」两个字", "答案" not in body)

    order = c.eval("""(() => {
      const q = document.querySelector('.turn-user');
      const a = document.querySelector('.answer-text');
      const box = document.querySelector('.composer-box');
      if (!q || !a || !box) return null;
      return { questionTop: Math.round(q.getBoundingClientRect().top),
               answerTop: Math.round(a.getBoundingClientRect().top),
               boxTop: Math.round(box.getBoundingClientRect().top) };
    })()""")
    check("提问气泡在解答之上", order and order["questionTop"] < order["answerTop"],
          f"{order['questionTop']} < {order['answerTop']}" if order else "元素缺失")
    check("解答整体位于输入框上方", order and order["answerTop"] < order["boxTop"],
          f"{order['answerTop']} < {order['boxTop']}" if order else "元素缺失")

    # ---------- 4. 继续追问 ----------
    follow = c.eval("Array.from(document.querySelectorAll('.suggest .chip')).map(e => e.innerText)")
    check("提问后常驻「继续追问」", "继续追问" in body)
    check("追问建议三条", len(follow or []) == 3, " / ".join(follow or []))
    check("追问与本次提问相关",
          all(any(k in s for k in ("奖助", "奖学金", "版本", "材料")) for s in (follow or [])),
          " / ".join(follow or []))

    # ---------- 5. 政策原文站内链接 ----------
    links = c.eval("""Array.from(document.querySelectorAll('.source-list a'))
      .map(a => ({ text: a.innerText.trim(), href: a.getAttribute('href') }))""")
    check("给出政策原文链接", bool(links), f"{len(links or [])} 条")
    check("链接指向站内知识库", all((l["href"] or "").startswith("/library?doc=") for l in (links or [])),
          (links or [{}])[0].get("href", ""))
    c.shot(str(out / "03-ask-answered.png"))

    # ---------- 6. 最近提问按钮折叠 ----------
    c.eval("document.querySelector('.history-toggle').click(); true")
    time.sleep(0.3)
    collapsed = c.eval("getComputedStyle(document.querySelector('.chat-side')).display")
    check("点「最近提问」收起右侧历史", collapsed == "none", f"display={collapsed}")
    c.eval("document.querySelector('.history-toggle').click(); true")
    time.sleep(0.3)
    expanded = c.eval("getComputedStyle(document.querySelector('.chat-side')).display")
    check("再点一次展开", expanded != "none", f"display={expanded}")

    # ---------- 7. 点历史只填入、不自动提交 ----------
    before = c.eval("document.querySelectorAll('.turn-user').length")
    title = c.eval("""(() => {
      const b = document.querySelector('.history-list button');
      if (!b) return '';
      const t = b.querySelector('.h-title').innerText;
      b.click();
      return t;
    })()""")
    time.sleep(1.2)
    value = c.eval("document.querySelector('.composer-box textarea').value")
    after = c.eval("document.querySelectorAll('.turn-user').length")
    thinking = c.eval("!!document.querySelector('.thinking')")
    check("点历史把问题填进输入框", value == title, f"{value!r}")
    check("没有自动提交", (not thinking) and after == before,
          f"气泡数 {before} → {after} · 推理中={thinking}")
    c.shot(str(out / "04-ask-history-filled.png"))

    # ---------- 7.5 原样重问：续在原会话里，不新增同题记录 ----------
    rows_before = c.eval("document.querySelectorAll('.history-list button').length")
    c.eval("document.querySelector('.send').click(); true")
    answered_again = c.wait_for("!!document.querySelector('.answer-text')", 150)
    time.sleep(0.8)
    rows_after = c.eval("document.querySelectorAll('.history-list button').length")
    check("原样重问续在原会话里（历史不新增一行）",
          answered_again and rows_after == rows_before,
          f"历史 {rows_before} → {rows_after} 行")

    # ---------- 8. 原文链接落到知识库对应文件 ----------
    href = (links or [{}])[0].get("href") or ""
    if href:
        c.goto(base + href)
        ok = c.wait_for("!!document.querySelector('.table tr.highlight')", 20)
        check("原文链接落到知识库并高亮该文件", ok, href)
        opened = c.eval("!!document.querySelector('.detail-row')")
        check("同时展开了该文件的片段", opened)
        c.shot(str(out / "05-library-target.png"))

    # ---------- 汇总 ----------
    failed = [item for item in checks if not item[1]]
    print(f"\n[ask-flow] {len(checks) - len(failed)} / {len(checks)} 通过")
    if failed:
        for name, _, detail in failed:
            print(f"  未通过：{name} {detail}")
        return 1
    print(f"[ask-flow] 截图目录 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
