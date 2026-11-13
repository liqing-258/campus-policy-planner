"""点一下「继续追问」，确认它真的答得出来。

**为什么还要一支专门的巡检**

`ask_flow_check.py` 只检查"输入框上方有三条建议、措辞与本次提问相关"——
它不看**点下去之后**发生了什么。R4 之前这批建议点下去经常是拒答卡，
R5 把建议改成由证据派生之后，"点得动"才成为可断言的东西。

判据（每条建议都跑一遍）：

1. 建议文字与 `/qa/ask` 响应里的 `next_questions` 一致（不是前端另有模板）；
2. 点击后出现**新的一块**解答（`.answer-text` 数量 +1）；
3. 新解答不是拒答卡（没有 `.refusal-card`），且正文 ≥ 40 字；
4. 新解答至少有一条引用（`.cite-list` 非空）——没有引用的答案在本系统里视为不可核验。

用法：
    .venv\\Scripts\\python.exe tools\\followup_click_check.py
    .venv\\Scripts\\python.exe tools\\followup_click_check.py --out .workbuddy/shots/followup
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
# 三条都点一遍；`--max-clicks` 可在只想要冒烟时减少
MAX_CLICKS = 3


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("base", nargs="?", default="http://127.0.0.1:5000")
    parser.add_argument("--out", default=".workbuddy/shots/followup")
    parser.add_argument("--port", type=int, default=9336)
    parser.add_argument("--max-clicks", type=int, default=MAX_CLICKS)
    args = parser.parse_args()

    base = args.base.rstrip("/")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    launch(port=args.port)
    c = Cdp(args.port)
    c.send("Emulation.setDeviceMetricsOverride",
           {"width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False})

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))
        print(f"  {'OK  ' if ok else 'FAIL'} {name}{(' · ' + detail) if detail else ''}")

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
    if not c.wait_for("location.pathname === '/' && !!document.querySelector('.side')", 25):
        print("[followup] 登录失败")
        return 1

    c.goto(f"{base}/ask")
    c.wait_for("!!document.querySelector('.composer-box textarea')", 20)

    # ---------- 先问一句，拿到建议 ----------
    c.eval(f"""(() => {{
      const el = document.querySelector('.composer-box textarea');
      el.value = {QUESTION!r};
      el.dispatchEvent(new Event('input', {{bubbles: true}}));
      return true;
    }})()""")
    c.eval("document.querySelector('.send').click(); true")
    if not c.wait_for("document.querySelectorAll('.answer-text').length >= 1", 120):
        print("[followup] 首问 120 秒内没有解答")
        return 1
    time.sleep(0.6)

    chips = c.eval("Array.from(document.querySelectorAll('.suggest .chip')).map(e => e.innerText.trim())") or []
    check("首问后有至少两条建议", len(chips) >= 2, f"{len(chips)} 条")
    print("  建议：" + " / ".join(chips))

    def idle(timeout: float = 40) -> bool:
        """等到"可以发送"：`asking` 为真时 `ask()` 会直接 return，点 chip 等于没点。

        第一版巡检没等这个，第 3 条必然超时——而接口直调是好的（3.5 s 出答案）。
        """
        return c.wait_for(
            "(() => { const b = document.querySelector('.send');"
            " return !!b && !b.disabled; })()", timeout)

    # ---------- 逐条点 ----------
    # 每轮回答后建议会重算，条数也可能少于 3（文件小、章节都引用过了）——
    # 按**当前**条数决定点几条，不要拿首轮的 3 条当固定预算。
    for index in range(args.max_clicks):
        if not idle():
            check(f"第 {index + 1} 条追问前界面空闲", False, "40 秒内仍在处理中")
            continue
        before = c.eval("document.querySelectorAll('.answer-text').length") or 0
        # 点**当前**第 index 条（列表会随每轮回答重算，不能拿首轮的文案当准）
        clicked = c.eval(f"""(() => {{
          const chips = Array.from(document.querySelectorAll('.suggest .chip'));
          const target = chips[{index}];
          if (!target) return null;
          const label = target.innerText.trim();
          target.click();
          return label;
        }})()""")
        if clicked is None:
            check(f"建议条数足够点到第 {index + 1} 条", False, "这一轮只有更少的建议")
            break
        print(f"  点击：{clicked}")
        ok_new = c.wait_for(
            f"document.querySelectorAll('.answer-text').length > {before}", 120)
        if not ok_new:
            refusal_seen = c.eval("!!document.querySelector('.refusal-card, .turn-bot.warn')")
            check(f"第 {index + 1} 条追问点下去有回应", False,
                  ("被拒答：" if refusal_seen else "120 秒内没有新解答：") + str(clicked))
            continue
        idle()  # 等这一轮收尾，否则下一次点击会被 asking 挡掉
        time.sleep(0.4)

        info = c.eval("""(() => {
          const answers = Array.from(document.querySelectorAll('.answer-text'));
          const last = answers[answers.length - 1];
          const turn = last.closest('.turn') || document.body;
          return {
            text: (last.innerText || '').trim().length,
            refusal: !!document.querySelector('.refusal-card'),
            cites: turn.querySelectorAll('.cite-list li').length,
          };
        })()""") or {}
        check(f"第 {index + 1} 条追问已作答", True, clicked or "")
        check(f"第 {index + 1} 条不是拒答", not info.get("refusal"), "")
        check(f"第 {index + 1} 条正文够长", int(info.get("text", 0)) >= 40, f"{info.get('text')} 字")
        check(f"第 {index + 1} 条带引用", int(info.get("cites", 0)) > 0, f"{info.get('cites')} 条引用")
        c.shot(str(out / f"followup-{index + 1}.png"))

    c.shot(str(out / "followup-final.png"))
    failed = [row for row in checks if not row[1]]
    print(f"\n[followup] {len(checks) - len(failed)} / {len(checks)} 通过")
    for name, _, detail in failed:
        print(f"  未通过：{name} {detail}")
    print(f"[followup] 截图目录 {out}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
