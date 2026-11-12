"""把提交版 Markdown 排成 A4 PDF，并数出真实页数。

为什么需要它：赛事要求"技术文档 PDF ≤30 页"。文档写成 Markdown 时，
"多少页"只能按字数估——而**估算不能当证据**。这个脚本做三件事：

1. 把 Markdown 渲染成 A4 打印样式的 HTML（表格、代码块、引用、标题层级）；
2. 用系统自带 Edge 的无头模式 `Page.printToPDF` 出 PDF（字体就用本机的雅黑）；
3. 用 PyMuPDF 打开 PDF 数页数，顺便抽每页首行做目视核对。

用法：
    python tools/md_to_pdf.py "docs/70-提交版/技术文档（提交版·30页）.md"
    python tools/md_to_pdf.py <md> --out docs/70-提交版/x.pdf --max-pages 30
"""

from __future__ import annotations

import argparse
import html
import http.server
import re
import socket
import socketserver
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(
    0, str(Path.home() / ".workbuddy" / "skills" / "headless-browser-verify-cdp" / "scripts")
)

from cdp import Cdp, launch  # noqa: E402

CSS = """
@page { size: A4; margin: 16mm 15mm 15mm 15mm; }
* { box-sizing: border-box; }
body {
  font-family: "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", sans-serif;
  font-size: 11.5pt; line-height: 1.85; color: #1a1a1a; margin: 0;
  -webkit-font-smoothing: antialiased;
}
h1 { font-size: 19pt; margin: 0 0 4pt; letter-spacing: .5pt; }
h2 { font-size: 15pt; margin: 16pt 0 6pt; padding-bottom: 3pt; border-bottom: 1.2pt solid #2b6cb0; color: #1a365d;
     page-break-after: avoid; }
h3 { font-size: 12.5pt; margin: 11pt 0 4pt; color: #2b4a6f; page-break-after: avoid; }
h4 { page-break-after: avoid; }
table, pre, blockquote { page-break-inside: avoid; }
h1 + h2 { margin-top: 6pt; }
p { margin: 5pt 0; text-align: justify; }
ul, ol { margin: 5pt 0 5pt 0; padding-left: 18pt; }
li { margin: 2.5pt 0; }
table { width: 100%; border-collapse: collapse; margin: 8pt 0; font-size: 10pt; page-break-inside: auto; }
th, td { border: .6pt solid #b9c2cc; padding: 3.2pt 4.5pt; vertical-align: top; line-height: 1.5; }
th { background: #eef3f9; font-weight: 600; white-space: nowrap; }
tr { page-break-inside: avoid; }
table.two td:first-child { width: 38%; }
table.two td { word-break: break-all; }
code { font-family: Consolas, "Courier New", monospace; font-size: 9pt;
       background: #f2f4f7; padding: 0 2pt; border-radius: 2pt; }
pre { background: #f7f9fb; border-left: 2.4pt solid #2b6cb0; padding: 6pt 8pt;
      overflow: hidden; page-break-inside: avoid; margin: 6pt 0; }
pre code { white-space: pre-wrap; word-break: break-all; }
pre code { background: none; font-size: 8.6pt; line-height: 1.5; }
blockquote { margin: 6pt 0; padding: 4pt 9pt; border-left: 2.4pt solid #a8b6c6;
             background: #fafbfc; color: #33404d; font-size: 9.6pt; }
blockquote p { margin: 2pt 0; }
hr { border: 0; border-top: .6pt solid #d3dae2; margin: 10pt 0; }
strong { font-weight: 600; }
img { max-width: 100%; }
.doc-foot { margin-top: 14pt; padding-top: 5pt; border-top: .6pt solid #d3dae2;
            font-size: 8.4pt; color: #7a8794; }
"""

INLINE_CODE = re.compile(r"`([^`]+)`")
BOLD = re.compile(r"\*\*([^*]+)\*\*")


def inline(text: str) -> str:
    """先转义再加格式——顺序反了会把标签也转义掉。

    `<br />` 不能先拼进去（会被 escape 成字面量），用占位符换回来。
    """
    parts = [html.escape(p, quote=False) for p in text.split("\x00")]
    joined = "<br />".join(parts)
    joined = INLINE_CODE.sub(lambda m: f"<code>{m.group(1)}</code>", joined)
    joined = BOLD.sub(lambda m: f"<strong>{m.group(1)}</strong>", joined)
    return joined


def split_row(line: str) -> list[str]:
    cells = line.strip().strip("|").split("|")
    return [c.strip() for c in cells]


def render(md: str) -> str:
    lines = md.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    i, n = 0, len(lines)

    while i < n:
        line = lines[i]
        stripped = line.strip()

        # 代码块（含 mermaid，PNG 般渲染成等宽块）
        if stripped.startswith("```"):
            lang = stripped[3:].strip()
            i += 1
            buf: list[str] = []
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1  # 跳过结束围栏
            caption = ""
            if lang in ("mermaid", "text", "bash"):
                note = {"mermaid": "图（mermaid 源码，PDF 中以等宽文本呈现）",
                        "bash": " shell", "text": " 文本"}.get(lang, "")
                caption = (f'<div style="font-size:8.4pt;color:#7a8794;margin:0 0 2pt">'
                           f'{html.escape(note)}</div>') if note else "" 
            out.append(f"<pre>{caption}<code>{html.escape(chr(10).join(buf))}</code></pre>")
            continue

        # 表格
        if stripped.startswith("|") and i + 1 < n and re.match(r"^\|[\s:|-]+\|$", lines[i + 1].strip()):
            head = split_row(lines[i])
            i += 2
            body: list[list[str]] = []
            while i < n and lines[i].strip().startswith("|"):
                body.append(split_row(lines[i]))
                i += 1
            cols = len(head)
            cls = " class=\"two\"" if cols == 2 else ""
            out.append(f"<table{cls}><thead><tr>"
                       + "".join(f"<th>{inline(c)}</th>" for c in head)
                       + "</tr></thead><tbody>")
            for row in body:
                row = (row + [""] * cols)[:cols]
                out.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in row) + "</tr>")
            out.append("</tbody></table>")
            continue

        # 标题
        m = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if m:
            level = len(m.group(1))
            out.append(f"<h{level}>{inline(m.group(2))}</h{level}>")
            i += 1
            continue

        # 分隔线
        if re.match(r"^-{3,}$", stripped) or re.match(r"^\*{3,}$", stripped):
            out.append("<hr />")
            i += 1
            continue

        # 引用块（连续 > 行合成一个 blockquote）
        if stripped.startswith(">"):
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip()[1:].strip())
                i += 1
            out.append("<blockquote><p>" + inline("\x00".join(buf)) + "</p></blockquote>")
            continue

        # 列表（- / 数字），只处理一层缩进
        if re.match(r"^[-*]\s+", stripped) or re.match(r"^\d+\.\s+", stripped):
            ordered = bool(re.match(r"^\d+\.\s+", stripped))
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>")
            while i < n:
                cur = lines[i].strip()
                m2 = re.match(r"^[-*]\s+(.*)$", cur) or re.match(r"^\d+\.\s+(.*)$", cur)
                if not m2:
                    break
                out.append(f"<li>{inline(m2.group(1))}</li>")
                i += 1
            out.append(f"</{tag}>")
            continue

        # 空行
        if not stripped:
            i += 1
            continue

        # 段落：吃到空行 / 特殊块为止
        buf = [stripped]
        i += 1
        while i < n:
            nxt = lines[i].strip()
            if (not nxt or nxt.startswith(("#", "|", ">", "```"))
                    or re.match(r"^[-*]\s+", nxt) or re.match(r"^\d+\.\s+", nxt)
                    or re.match(r"^-{3,}$", nxt)):
                break
            buf.append(nxt)
            i += 1
        out.append("<p>" + inline("\x00".join(buf)) + "</p>")

    return "\n".join(out)


def serve(directory: Path) -> tuple[socketserver.TCPServer, int]:
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(directory), **kw)

        def log_message(self, *a):
            pass

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    httpd = socketserver.TCPServer(("127.0.0.1", port), Quiet)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("md")
    ap.add_argument("--out", default=None)
    ap.add_argument("--max-pages", type=int, default=30)
    ap.add_argument("--port", type=int, default=9345)
    args = ap.parse_args()

    src = Path(args.md)
    if not src.is_absolute():
        src = ROOT / src
    out_pdf = Path(args.out) if args.out else src.with_suffix(".pdf")
    if not out_pdf.is_absolute():
        out_pdf = ROOT / out_pdf

    body = render(src.read_text(encoding="utf-8"))
    page = (f'<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8" />'
            f"<title>{html.escape(src.stem)}</title><style>{CSS}</style></head>"
            f"<body>{body}</body></html>")

    work = Path(tempfile.mkdtemp(prefix="zxp_pdf_"))
    (work / "index.html").write_text(page, encoding="utf-8")
    httpd, port = serve(work)
    print(f"[PDF] HTML 已渲染（{len(page):,} 字节），本地端口 {port}")

    proc = launch(port=args.port, user_dir=str(work / "ud"))
    if proc is None:
        print("[PDF] 找不到 Edge/Chrome，无法出 PDF")
        return 2
    try:
        cdp = Cdp(port=args.port)
        cdp.goto(f"http://127.0.0.1:{port}/index.html", timeout=40)
        cdp.eval("document.fonts && document.fonts.ready.then(()=>1)")
        res = cdp.send("Page.printToPDF", {
            "printBackground": True,
            "paperWidth": 8.27, "paperHeight": 11.69,
            "marginTop": 0.0, "marginBottom": 0.0,
            "marginLeft": 0.0, "marginRight": 0.0,
            "preferCSSPageSize": True,
        }, timeout=120)
        import base64
        data = base64.b64decode(res["data"])
        out_pdf.parent.mkdir(parents=True, exist_ok=True)
        out_pdf.write_bytes(data)
        cdp.close()
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
        httpd.shutdown()

    import fitz  # PyMuPDF

    doc = fitz.open(out_pdf)
    pages = doc.page_count
    print(f"[PDF] {out_pdf.relative_to(ROOT)}")
    print(f"[PDF] 体积 {len(data) / 1024:.1f} KB · 页数 {pages} · 上限 {args.max_pages}")
    heads = []
    for idx in range(pages):
        text = doc[idx].get_text().strip().splitlines()
        heads.append(f"      p{idx + 1:<2} {(text[0][:38] if text else '(空)')}")
    print("[PDF] 每页首行（目视核对是否断在奇怪的地方）：")
    print("\n".join(heads))
    doc.close()

    if pages > args.max_pages:
        print(f"[FAIL] 页数 {pages} 超过上限 {args.max_pages}")
        return 1
    print(f"[OK] {pages} 页 ≤ {args.max_pages} 页，余量 {args.max_pages - pages} 页")
    return 0


if __name__ == "__main__":
    sys.exit(main())