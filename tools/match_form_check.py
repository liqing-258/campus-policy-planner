"""资格匹配页表单巡检：把"数值字段不能填负数""学生类型要能选"变成可执行断言。

为什么单独写一个脚本，而不是靠肉眼看截图：
这一版最初的实现用了 `type="number"` + `v-model`，浏览器里手敲 `-4` 时
Vue 会把值转成 number 写回，接着 `.replace()` / `.trim()` 抛 TypeError，
**整个 `.profile-grid` 从页面上消失**——肉眼只看到"表单没了"，
根本猜不到是类型问题。下面这些断言专门盯着这条路径：

1. 学生类型是下拉，且含本科/专科等中文选项
2. 数值字段里不存在 `type="number"`（原生上下箭头连带消失，不用改颜色）
3. 往年级/绩点/已修学分里塞 `-4` 这类粘贴值：页面不崩、负号被抹掉
4. 塞越界值 `99`：给出红字提示、禁用提交、**但不把 99 悄悄改成 8**
5. 改成合法值：提示消失、按钮恢复可点
6. 失焦只统一写法（`007` → `7`），不改数值
7. 全程零 JS 异常

用法：
    python tools/match_form_check.py --out .workbuddy/shots/matchform
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

# 用户点名的三个"不可能为负"的字段
NUMERIC_LABELS = ("年级", "平均学分绩点", "已修学分")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("base", nargs="?", default="http://127.0.0.1:5000")
    parser.add_argument("--out", default=".workbuddy/shots/matchform")
    parser.add_argument("--port", type=int, default=9337)
    args = parser.parse_args()

    base = args.base.rstrip("/")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    launch(port=args.port)
    c = Cdp(args.port)
    c.send("Emulation.setDeviceMetricsOverride",
           {"width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False})
    # 无头窗口没有系统焦点时 focus/blur 事件不派发，失焦用例会假失败
    c.send("Emulation.setFocusEmulationEnabled", {"enabled": True})

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
    c.wait_for("location.pathname === '/' && !!document.querySelector('.side')", 25)

    c.goto(f"{base}/match")
    if not c.wait_for("!!document.querySelector('.profile-grid')", 25):
        print("  FAIL 打不开资格匹配页（.profile-grid 不存在）")
        return 1
    time.sleep(0.8)
    c.clear_events()

    # 页面里按 label 文本取字段：字段顺序改了就换位置，靠索引会静默测错字段
    helper = """window.__f = (label) => Array.from(document.querySelectorAll('.profile-grid .field'))
        .find(el => el.querySelector('span') && el.querySelector('span').textContent.trim() === label);"""

    def field_js(label: str, prop: str = "value") -> str:
        return f"(() => {{ {helper} const f = window.__f({label!r}); return f ? f.querySelector('input,select').{prop} : null; }})()"

    # ---------- 1. 学生类型是下拉 ----------
    st = c.eval(f"""(() => {{
      {helper}
      const f = window.__f('学生类型');
      if (!f) return null;
      const el = f.querySelector('input,select');
      return {{ tag: el.tagName, options: Array.from(el.options || []).map(o => o.textContent.trim()) }};
    }})()""")
    check("学生类型是下拉选择", bool(st) and st["tag"] == "SELECT", f"<{st['tag'] if st else '?'}>")
    opts = (st or {}).get("options") or []
    check("下拉含「全日制在校本科生」", "全日制在校本科生" in opts, f"{len(opts)} 项")
    check("下拉含「全日制在校专科生」", "全日制在校专科生" in opts)

    # ---------- 2. 数值字段不再用 type=number ----------
    nums = c.eval("document.querySelectorAll('.profile-grid input[type=\"number\"]').length")
    check("数值框里没有原生 number（上下箭头结构上不存在）", nums == 0, f"{nums} 个")
    modes = c.eval(f"""(() => {{
      {helper}
      return {list(NUMERIC_LABELS)!r}.map(label => {{
        const el = window.__f(label).querySelector('input');
        return {{ label, type: el.type, inputmode: el.getAttribute('inputmode') }};
      }});
    }})()""")
    check("三个数值框都保留数字键盘提示",
          all(m["inputmode"] == "decimal" and m["type"] == "text" for m in (modes or [])),
          str([(m["label"], m["type"], m["inputmode"]) for m in (modes or [])]))

    def type_into(label: str, value: str, blur: bool = False) -> None:
        """模拟粘贴/脚本赋值：直接设 value 再派发 input（键盘输入走的也是这条路径）"""
        c.eval(f"""(() => {{
          {helper}
          const el = window.__f({label!r}).querySelector('input');
          el.value = {value!r};
          el.dispatchEvent(new Event('input', {{bubbles: true}}));
          return true;
        }})()""")
        if blur:
            c.eval(f"""(() => {{
              {helper}
              const el = window.__f({label!r}).querySelector('input');
              el.focus(); el.blur(); return true;
            }})()""")
            time.sleep(0.15)

    # ---------- 3. 负数：三个字段逐个试 ----------
    for label in NUMERIC_LABELS:
        type_into(label, "-4")
        got = c.eval(field_js(label))
        alive = c.eval("!!document.querySelector('.profile-grid')")
        check(f"{label} 填 -4 不崩溃", bool(alive), "表单仍在")
        check(f"{label} 的负号被抹掉", got is not None and "-" not in str(got), f"值={got!r}")

    errs = c.errors()
    check("负数输入过程无 JS 异常", not errs, "；".join(errs)[:200])
    c.shot(str(out / "01-negative-clean.png"))

    # ---------- 4. 越界：不静默纠正 ----------
    c.clear_events()
    type_into("年级", "99", blur=True)
    state = c.eval(f"""(() => {{
      {helper}
      const f = window.__f('年级');
      const line = document.querySelector('.invalid-line');
      const btn = document.querySelector('.card .actions button.btn');
      return {{
        value: f.querySelector('input').value,
        bad: f.querySelector('input').classList.contains('bad'),
        line: line ? line.innerText.replace(/\\s+/g, ' ').trim() : '',
        disabled: btn ? btn.disabled : null,
      }};
    }})()""")
    check("越界值原样保留（不被悄悄改成 8）", state["value"] == "99", f"值={state['value']!r}")
    check("越界字段被描红", state["bad"])
    check("给出越界提示文字", "年级" in state["line"] and "超出允许范围" in state["line"], state["line"][:80])
    check("越界时禁用提交", state["disabled"] is True, f"disabled={state['disabled']}")
    c.shot(str(out / "02-out-of-range.png"))

    # ---------- 5. 改回合法值 ----------
    c.clear_events()
    type_into("年级", "3")
    type_into("平均学分绩点", "3.25")
    type_into("已修学分", "120")
    time.sleep(0.3)
    ok_state = c.eval("""({
      line: !!document.querySelector('.invalid-line'),
      disabled: document.querySelector('.card .actions button.btn').disabled,
    })""")
    check("合法值后提示消失", not ok_state["line"])
    check("合法值后可以提交", ok_state["disabled"] is False, f"disabled={ok_state['disabled']}")

    # ---------- 6. 失焦只统一写法 ----------
    type_into("年级", "007", blur=True)
    normalized = c.eval(field_js("年级"))
    check("失焦把 007 归一成 7", normalized == "7", f"值={normalized!r}")

    errs = c.errors()
    check("全程无 JS 异常", not errs, "；".join(errs)[:200])
    c.shot(str(out / "03-form-valid.png"))

    # ---------- 汇总 ----------
    failed = [item for item in checks if not item[1]]
    print(f"\n[match-form] {len(checks) - len(failed)} / {len(checks)} 通过")
    if failed:
        for name, _, detail in failed:
            print(f"  未通过：{name} {detail}")
        return 1
    print(f"[match-form] 截图目录 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
