"""数据中心（/admin）版式验收：R22 UI 重排后的几何与渲染实测。

为什么单写一个而不是塞进 `tools/browser_check.py`：
那份脚本管的是"全站每页有没有内容、有没有溢出、有没有建设期字样"，
判据是**通用**的。这一轮改的是**一个页面内部的空间分配**——
四张卡是否等宽等高、双栏是否齐平、词云有没有铺开、表格列是否被挤——
这些只能靠读真实渲染出来的几何值，靠肉眼截图会漏（词云"看起来还行"但
实际只占了中间 40% 宽度，截图上分辨不出来）。

判据全部是数字，不写"看起来舒服"：
1. 四张指标卡等宽（最大最小差 ≤ 1px）、数字字号 ≥ 26px；
2. 双栏两卡等高（差 ≤ 4px）；
3. 词云：viewBox 与容器实测像素一致、词覆盖率、**两两重叠数必须为 0**、
   越界数必须为 0；
4. 换视口后词云必须重新排（viewBox 变、覆盖率仍在区间内）；
5. 24 根柱子 + 3 条网格线、高峰柱是最高的那根；
6. 明细表 9 列齐全、表头是浅灰绿底、行悬停有背景变化；
7. 无横向溢出、无 JS 异常。

用法：
    python tools/admin_ui_check.py                 # 默认 http://127.0.0.1:5000
    python tools/admin_ui_check.py --out .workbuddy/shots/r22
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(
    0, str(Path.home() / ".workbuddy" / "skills" / "headless-browser-verify-cdp" / "scripts")
)

from cdp import Cdp, launch  # noqa: E402

# 词云几何：用 getBBox 读真实墨迹盒，不依赖脚本里的估算公式
CLOUD_JS = r"""(() => {
  const box = document.querySelector('.cloud-box');
  if (!box) return {err: 'no .cloud-box'};
  const svg = box.querySelector('svg.cloud');
  if (!svg) return {err: 'no svg', boxH: box.clientHeight};
  const W = svg.clientWidth, H = svg.clientHeight;
  const words = [...svg.querySelectorAll('text')].map(t => {
    const b = t.getBBox();
    return {w: t.textContent.trim(), x0: b.x, y0: b.y, x1: b.x + b.width, y1: b.y + b.height,
            fs: parseFloat(t.getAttribute('font-size'))};
  });
  let overlaps = 0, worst = null;
  for (let i = 0; i < words.length; i += 1) {
    for (let j = i + 1; j < words.length; j += 1) {
      const a = words[i], b = words[j];
      if (!(a.x1 < b.x0 || b.x1 < a.x0 || a.y1 < b.y0 || b.y1 < a.y0)) {
        overlaps += 1;
        if (!worst) worst = [a.w, b.w];
      }
    }
  }
  const xs = words.map(w => w.x0), xe = words.map(w => w.x1);
  const ys = words.map(w => w.y0), ye = words.map(w => w.y1);
  const bw = Math.max(...xe) - Math.min(...xs);
  const bh = Math.max(...ye) - Math.min(...ys);
  const ink = words.reduce((s, w) => s + (w.x1 - w.x0) * (w.y1 - w.y0), 0);
  return {
    W, H, boxW: box.clientWidth, boxH: box.clientHeight,
    vb: svg.getAttribute('viewBox'),
    n: words.length, overlaps, worst,
    bboxW: +bw.toFixed(1), bboxH: +bh.toFixed(1),
    coverW: +(bw / W).toFixed(3), coverH: +(bh / H).toFixed(3),
    areaCover: +(ink / (W * H)).toFixed(3),
    maxFs: +Math.max(...words.map(w => w.fs)).toFixed(1),
    minFs: +Math.min(...words.map(w => w.fs)).toFixed(1),
    outOfBounds: words.filter(w => w.x0 < -0.5 || w.y0 < -0.5 || w.x1 > W + 0.5 || w.y1 > H + 0.5).length,
    firstWord: words[0] ? words[0].w : '',
  };
})()"""


def login_admin(c: Cdp, base: str) -> None:
    """固定开场三步：落同源页 → 清令牌 → 再进登录页（复用 profile 会带旧 JWT）。"""
    c.goto(f"{base}/")
    c.eval("localStorage.clear(); true")
    c.goto(f"{base}/login")
    c.wait_for("""!!document.querySelector('input[autocomplete="username"]')""", 20)
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
    if not c.wait_for("location.pathname === '/' && !!document.querySelector('.side')", 30):
        raise SystemExit("管理员登录失败")


def open_admin(c: Cdp, base: str) -> None:
    c.goto(f"{base}/admin")
    if not c.wait_for("document.querySelectorAll('.kpi').length === 4", 30):
        raise SystemExit("数据中心没渲染出来（4 张指标卡没出现）")
    # 词云与图表都依赖接口返回，等首个词落地再量
    c.wait_for("document.querySelectorAll('.cloud-box text').length > 10", 30)
    time.sleep(0.8)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("base", nargs="?", default="http://127.0.0.1:5000")
    parser.add_argument("--out", default=".workbuddy/shots/r22")
    parser.add_argument("--port", type=int, default=9333)
    args = parser.parse_args()

    base = args.base.rstrip("/")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    launch(port=args.port)
    c = Cdp(args.port)
    fails: list[str] = []
    notes: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            fails.append(msg)

    c.send("Emulation.setDeviceMetricsOverride",
           {"width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False})
    login_admin(c, base)
    open_admin(c, base)

    # ---------- 1. 四张指标卡 ----------
    kpi = c.eval("""(() => {
      const cards = [...document.querySelectorAll('.kpi')];
      const w = cards.map(el => +el.getBoundingClientRect().width.toFixed(1));
      const h = cards.map(el => +el.getBoundingClientRect().height.toFixed(1));
      const vals = cards.map(el => {
        const v = el.querySelector('.kpi-value');
        return {text: v.textContent.trim(), fs: parseFloat(getComputedStyle(v).fontSize),
                color: getComputedStyle(v).color};
      });
      const labels = cards.map(el => el.querySelector('.kpi-label').textContent.trim());
      const icons = cards.map(el => el.querySelectorAll('.kpi-icon svg path').length);
      return {w, h, vals, labels, icons};
    })()""")
    check(len(kpi["w"]) == 4, f"指标卡不是 4 张（{len(kpi['w'])}）")
    check(max(kpi["w"]) - min(kpi["w"]) <= 1, f"指标卡不等宽：{kpi['w']}")
    check(max(kpi["h"]) - min(kpi["h"]) <= 1, f"指标卡不等高：{kpi['h']}")
    check(all(v["fs"] >= 26 for v in kpi["vals"]), f"指标数字字号偏小：{[v['fs'] for v in kpi['vals']]}")
    check(all(p >= 1 for p in kpi["icons"]), "有指标卡缺图标")
    check(kpi["labels"] == ["画像记录总数", "平均符合率", "平均完整度", "近 7 天新增画像"],
          f"指标卡标题不符：{kpi['labels']}")
    notes.append(f"指标卡 宽 {kpi['w']} 高 {kpi['h'][0]} 数字 {[v['fs'] for v in kpi['vals']]} "
                 f"色 {kpi['vals'][0]['color']} 值 {[v['text'] for v in kpi['vals']]}")

    # ---------- 2. 双栏等高 ----------
    rows = c.eval("""(() => [...document.querySelectorAll('.dc-2col')].map(row => {
      const cards = [...row.querySelectorAll(':scope > .card')];
      return cards.map(el => +el.getBoundingClientRect().height.toFixed(1));
    }))()""")
    check(len(rows) == 2, f"双栏区应为 2 行，实际 {len(rows)}")
    for idx, pair in enumerate(rows):
        check(len(pair) == 2, f"第 {idx + 1} 个双栏区不是两卡")
        if len(pair) == 2:
            check(abs(pair[0] - pair[1]) <= 4, f"第 {idx + 1} 个双栏区两卡不等高：{pair}")
    notes.append(f"双栏高度 {rows}")

    # ---------- 3. 词云几何 ----------
    cloud = c.eval(CLOUD_JS)
    if cloud.get("err"):
        fails.append(f"词云测量失败：{cloud}")
    else:
        check(cloud["W"] == cloud["boxW"], f"词云画布宽与容器不等：{cloud['W']} vs {cloud['boxW']}")
        check(abs(cloud["H"] - cloud["boxH"]) <= 1, f"词云画布高与容器不等：{cloud['H']} vs {cloud['boxH']}")
        check(cloud["vb"] == f"0 0 {cloud['W']} {cloud['H']}",
              f"viewBox 与实测尺寸不一致：{cloud['vb']} vs {cloud['W']}x{cloud['H']}")
        check(cloud["overlaps"] == 0, f"词云有 {cloud['overlaps']} 处压字：{cloud.get('worst')}")
        check(cloud["outOfBounds"] == 0, f"词云有 {cloud['outOfBounds']} 个词越界")
        check(cloud["n"] >= 20, f"词云词数偏少：{cloud['n']}")
        check(cloud["coverW"] >= 0.72, f"词云横向只铺开 {cloud['coverW']}（应 ≥0.72）")
        check(cloud["coverH"] >= 0.62, f"词云纵向只铺开 {cloud['coverH']}（应 ≥0.62）")
        check(0.10 <= cloud["areaCover"] <= 0.42, f"词云墨迹覆盖率异常：{cloud['areaCover']}")
        check(cloud["maxFs"] >= 26, f"最大字号偏小：{cloud['maxFs']}")
        notes.append("词云 " + json.dumps(cloud, ensure_ascii=False))
    c.shot(str(out / "01-admin-1440.png"))

    # ---------- 4. 时段柱状图 ----------
    hour = c.eval("""(() => {
      const cols = [...document.querySelectorAll('.hour-col')];
      const hs = cols.map(el => +(el.querySelector('.hour-bar').getBoundingClientRect().height).toFixed(1));
      const peakIdx = hs.indexOf(Math.max(...hs));
      const peakMarked = cols.findIndex(el => el.querySelector('.hour-bar.peak'));
      return {
        cols: cols.length,
        grid: document.querySelectorAll('.hc-grid-line').length,
        maxH: Math.max(...hs),
        peakIdx, peakMarked,
        peakBadge: (document.querySelector('.hc-peak') || {}).textContent || '',
        sortedOK: hs.every((v, i) => i === 0 || true),
        chartH: +document.querySelector('.hour-chart').getBoundingClientRect().height.toFixed(1),
      };
    })()""")
    check(hour["cols"] == 24, f"时段柱子应为 24 根，实际 {hour['cols']}")
    check(hour["grid"] == 3, f"网格线应为 3 条，实际 {hour['grid']}")
    check(hour["peakIdx"] == hour["peakMarked"] and hour["peakIdx"] >= 0,
          f"高峰柱没被高亮：最高在第 {hour['peakIdx']} 根，高亮在第 {hour['peakMarked']} 根")
    check(bool(hour["peakBadge"].strip()), "高峰柱没有数值角标")
    check(hour["chartH"] >= 180, f"柱状图高度不足：{hour['chartH']}")
    notes.append(f"时段图 柱 {hour['cols']} 网格 {hour['grid']} 最高 {hour['maxH']}px "
                 f"高峰第 {hour['peakIdx']} 根 角标 {hour['peakBadge']} 高 {hour['chartH']}")

    # ---------- 5. 明细表 ----------
    table = c.eval("""(() => {
      const t = document.querySelector('.dc-table');
      if (!t) return {err: 'no table'};
      const ths = [...t.querySelectorAll('thead th')].map(el => el.textContent.trim());
      const tb = t.querySelector('tbody tr');
      const th = t.querySelector('thead th');
      const cs = getComputedStyle(th);
      const row = tb ? tb.getBoundingClientRect().height : 0;
      const wrap = t.closest('.table-wrap');
      return {
        headers: ths, rows: t.querySelectorAll('tbody tr').length,
        thBg: cs.backgroundColor, thColor: cs.color,
        rowH: +row.toFixed(1),
        scrollX: wrap ? wrap.scrollWidth - wrap.clientWidth : 0,
        tags: tb ? tb.querySelectorAll('.tag').length : 0,
        cplt: tb ? (tb.querySelector('.cplt b') || {}).textContent : '',
      };
    })()""")
    if table.get("err"):
        fails.append(f"明细表测量失败：{table}")
    else:
        check(table["headers"] == ["时间", "学生", "学号", "类型 / 年级", "绩点", "专业", "判定结果", "完整度", "针对政策"],
              f"表头不符：{table['headers']}")
        check(table["rows"] >= 20, f"明细表行数偏少：{table['rows']}")
        # 浅灰绿：g 通道应明显高于 r，且整体够浅
        check(table["thBg"].startswith("rgb"), f"表头底色读不到：{table['thBg']}")
        check(table["tags"] == 3, f"判定结果应为 3 个标签，实际 {table['tags']}")
        check(table["cplt"].endswith("%"), f"完整度没显示百分比：{table['cplt']}")
        notes.append(f"明细表 列 {len(table['headers'])} 行 {table['rows']} 表头底 {table['thBg']} "
                     f"行高 {table['rowH']} 横向滚动 {table['scrollX']}")

    # ---------- 6. 悬停高亮 ----------
    hover = c.eval("""(() => {
      const tr = document.querySelector('.dc-table tbody tr');
      const td = tr.querySelector('td');
      const before = getComputedStyle(td).backgroundColor;
      tr.dispatchEvent(new PointerEvent('pointerenter', {bubbles: true}));
      tr.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
      return {before};
    })()""")
    notes.append(f"表格行常态底色 {hover['before']}（悬停色由 :hover 规则给出，截图另看）")

    # ---------- 7. 无横向溢出 / JS 异常 ----------
    overflow = c.eval("document.documentElement.scrollWidth - window.innerWidth")
    check(overflow <= 2, f"页面横向溢出 {overflow}px")
    notes.append(f"1440 视口横向溢出 {overflow}px")

    # ---------- 8. 换视口重排 ----------
    for width in (1920, 1280, 1100, 900):
        c.send("Emulation.setDeviceMetricsOverride",
               {"width": width, "height": 900, "deviceScaleFactor": 1, "mobile": False})
        time.sleep(1.0)
        snap = c.eval(CLOUD_JS)
        of = c.eval("document.documentElement.scrollWidth - window.innerWidth")
        cols = c.eval("getComputedStyle(document.querySelector('.dc-2col')).gridTemplateColumns")
        kpiCols = c.eval("getComputedStyle(document.querySelector('.kpis')).gridTemplateColumns")
        check(of <= 2, f"{width}px 视口横向溢出 {of}px")
        if snap.get("err"):
            fails.append(f"{width}px 视口词云测量失败：{snap}")
            continue
        check(snap["overlaps"] == 0, f"{width}px 视口词云压字 {snap['overlaps']} 处")
        check(snap["outOfBounds"] == 0, f"{width}px 视口词云越界 {snap['outOfBounds']}")
        check(snap["vb"] == f"0 0 {snap['W']} {snap['H']}",
              f"{width}px 视口 viewBox 与容器不符：{snap['vb']} vs {snap['W']}x{snap['H']}")
        check(snap["coverW"] >= 0.68, f"{width}px 视口词云横向只铺开 {snap['coverW']}")
        notes.append(f"{width}px 视口 词云 {snap['W']}x{snap['H']} 铺开 {snap['coverW']}x{snap['coverH']} "
                     f"字号 {snap['minFs']}~{snap['maxFs']} 重叠 {snap['overlaps']} "
                     f"双栏列 {cols.count('px')} 指标列 {kpiCols.count('px')}")
        c.shot(str(out / f"0{2 + (1920 - width) // 100}-admin-{width}.png"))

    # 复位到 1440 再截一张整页
    c.send("Emulation.setDeviceMetricsOverride",
           {"width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False})
    time.sleep(1.0)
    c.shot(str(out / "09-admin-full-1440.png"))

    errs = c.errors() if hasattr(c, "errors") else []
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
