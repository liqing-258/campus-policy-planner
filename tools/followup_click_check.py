"""点一下「继续追问」，确认它真的答得出来。

**为什么还要一支专门的巡检**

`ask_flow_check.py` 只检查"输入框上方有三条建议、措辞与本次提问相关"——
它不看**点下去之后**发生了什么。R4 之前这批建议点下去经常是拒答卡，
R5 把建议改成由证据派生之后，"点得动"才成为可断言的东西。

判据（每条建议都跑一遍）：

1. 点建议条**只把问题填进输入框**、不自动发送（R8 的用户要求：想先改两个字再问）；
2. 填进去的文字与这条建议完全一致（不是点了个空）；
3. 点「发送」后出现**新的一块**解答（`.answer-text` 数量 +1）；
4. 新解答不是拒答卡（没有 `.refusal-card`），且正文 ≥ 40 字；
5. 新解答至少有一条引用（`.cite-list` 非空）——没有引用的答案在本系统里视为不可核验。

**本脚本 R24 改过一次判定**：R8 把"点建议即发送"改成"点建议只填入"之后，本脚本仍按旧行为
`chip.click()` 之后直接等 `.answer-text` 增加，于是每一轮都空等满 120 秒，报"点下去没回应"。
那不是产品缺陷——**脚本假设了一个已经不存在的交互**。现在按真实交互走（点选 → 校验填入 → 点发送），
并新增第 1、2 条断言把这个契约本身咬住：一旦有人把"只填入"改回"直接发"，这里会立刻报出来。

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
    c.goto(f"{base}/login?open=1")
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

    # **先清掉会话缓存再进问答页**。
    # `launch()` 复用同一个浏览器 profile，上一轮跑完的那段对话会被
    # `sessionStorage['campus_qa_session']` 恢复出来——于是"等到出现第一块解答"这个条件
    # 在页面刚打开时就已成立，脚本立刻往下走，读到的是**还没重算的建议栏（0 条）**。
    # 这与 R21 记过的"断言等到了页面上本来就有的东西"是同一类错误：
    # 判据必须只对"本次操作产生的结果"成立，所以先把起点清干净。
    c.eval("sessionStorage.clear(); true")
    c.goto(f"{base}/ask")
    c.wait_for("!!document.querySelector('.composer-box textarea')", 20)
    c.wait_for("document.querySelectorAll('.answer-text').length === 0", 10)

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
        # 点**当前**第 index 条（列表会随每轮回答重算，不能拿首轮的文案当准）。
        # 返回标签 + 点击后输入框里的值 + 解答块数量，一次取全，省一轮往返。
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
        # 第 1、2 条断言：只填入、不发出去，且填的确实是这句话。
        #
        # **必须在点击之后单独等一次 DOM**：`useSuggestion()` 改的是 `question` 这个 ref，
        # 而 textarea 上的 `v-model` 要到下一帧（`nextTick`）才把值写回去。
        # 第一版把"点击 + 读值"放在同一个 eval 里，读到的是**点击前的旧值**，
        # 于是三条都判"没填进去"——而实际上框里已经换了字（后一条的 detail 里看得见）。
        # 这是断言时机问题，不是交互问题。
        ok_fill = c.wait_for(
            "(() => { const b = document.querySelector('.composer-box textarea');"
            f" return !!b && b.value.trim() === {clicked!r}; }})()", 5)
        filled = c.eval("(() => { const b = document.querySelector('.composer-box textarea');"
                        " return b ? b.value.trim() : ''; })()")
        after_click = c.eval("document.querySelectorAll('.answer-text').length")
        check(f"第 {index + 1} 条点选后只填入不发送",
              ok_fill and after_click == before,
              f"输入框={str(filled)[:24]!r} 解答块 {before}→{after_click}")
        # 第 3 步：真实用户接下来按的就是「发送」，等的是它之后的新解答
        c.eval("document.querySelector('.send').click(); true")
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

    # ---------- 边界场景（R24 补） ----------
    #
    # 追问链路正常之后，还有五种"真人一定会碰到"的情形：空白输入、连点、刷新、
    # 换角色。它们各自对应一个前端守卫（按钮禁用 / asking 互斥 / sessionStorage /
    # 登出清缓存），不验就只能靠"看起来没问题"。
    print("\n  -- 边界场景 --")
    baseline = c.eval("document.querySelectorAll('.answer-text').length") or 0

    # ① 空白输入：按钮应当禁用，点下去也不产生任何东西
    c.eval("""(() => {
      const el = document.querySelector('.composer-box textarea');
      el.value = '   ';
      el.dispatchEvent(new Event('input', {bubbles: true}));
      return true;
    })()""")
    time.sleep(0.3)
    blank_disabled = c.eval("document.querySelector('.send').disabled")
    c.eval("document.querySelector('.send').click(); true")
    time.sleep(1.2)
    after_blank = c.eval("document.querySelectorAll('.answer-text').length")
    check("空白输入：发送按钮禁用、点了也发不出去",
          bool(blank_disabled) and after_blank == baseline,
          f"disabled={blank_disabled} 解答块 {baseline}→{after_blank}")

    # ② 快速连点：`asking` 必须挡住第二、三次（否则一次提问会烧三次模型调用）
    c.eval(f"""(() => {{
      const el = document.querySelector('.composer-box textarea');
      el.value = '图书馆一次可以借几本书？';
      el.dispatchEvent(new Event('input', {{bubbles: true}}));
      return true;
    }})()""")
    c.wait_for("(() => { const b = document.querySelector('.send'); return !!b && !b.disabled; })()", 8)
    c.eval("""(() => {
      const b = document.querySelector('.send');
      b.click(); b.click(); b.click();
      return true;
    })()""")
    got_one = c.wait_for(
        f"document.querySelectorAll('.answer-text').length > {baseline}", 120)
    time.sleep(2.5)  # 给"如果真发了第二第三次"足够时间浮出来
    after_click = c.eval("document.querySelectorAll('.answer-text').length")
    check("快速连点三次只产生一条新解答",
          got_one and after_click == baseline + 1,
          f"{baseline}→{after_click}（期望 {baseline + 1}）")

    # ③ 整页刷新：对话要还在（`sessionStorage` 的职责，不是内存）
    before_reload = after_click
    c.goto(f"{base}/ask")
    c.wait_for("!!document.querySelector('.composer-box textarea')", 20)
    persisted = c.wait_for(
        f"document.querySelectorAll('.answer-text').length === {before_reload}", 15)
    now_count = c.eval("document.querySelectorAll('.answer-text').length")
    check("刷新后整段对话仍在（不是只剩内存）",
          persisted, f"刷新前 {before_reload} → 刷新后 {now_count}")

    # ④ 换角色：登出后以管理员登录，**不该看到学生的对话**
    c.eval("localStorage.clear(); sessionStorage.clear(); true")
    c.goto(f"{base}/login?open=1")
    c.wait_for("!!document.querySelector('input[autocomplete=\"username\"]')", 20)
    c.eval("""(() => {
      const u = document.querySelector('input[autocomplete="username"]');
      const p = document.querySelector('input[autocomplete="current-password"]');
      const set = (el, v) => {
        el.value = v;
        el.dispatchEvent(new Event('input', {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
      };
      set(u, 'admin'); set(p, 'admin123');
      document.querySelector('form .submit').click();
      return true;
    })()""")
    ok_admin = c.wait_for("location.pathname === '/' && !!document.querySelector('.side')", 25)
    check("管理员登录成功", ok_admin, "")
    c.goto(f"{base}/ask")
    c.wait_for("!!document.querySelector('.composer-box textarea')", 20)
    leaked = c.eval("document.querySelectorAll('.answer-text').length")
    check("换角色后看不到上一个用户的对话", int(leaked or 0) == 0, f"{leaked} 块")

    # ⑤ 管理员也能正常问答（两个角色走同一条链路，不是只有学生能用）
    c.eval(f"""(() => {{
      const el = document.querySelector('.composer-box textarea');
      el.value = '图书馆借的书逾期了会怎么处理？';
      el.dispatchEvent(new Event('input', {{bubbles: true}}));
      return true;
    }})()""")
    c.wait_for("(() => { const b = document.querySelector('.send'); return !!b && !b.disabled; })()", 8)
    c.eval("document.querySelector('.send').click(); true")
    admin_ok = c.wait_for("document.querySelectorAll('.answer-text').length >= 1", 120)
    admin_len = c.eval("(() => { const a = document.querySelectorAll('.answer-text'); return a.length ? a[a.length-1].innerText.trim().length : 0; })()")
    check("管理员提问也能得到解答", admin_ok and int(admin_len or 0) >= 40, f"{admin_len} 字")

    c.shot(str(out / "followup-final.png"))
    failed = [row for row in checks if not row[1]]
    print(f"\n[followup] {len(checks) - len(failed)} / {len(checks)} 通过")
    for name, _, detail in failed:
        print(f"  未通过：{name} {detail}")
    print(f"[followup] 截图目录 {out}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
