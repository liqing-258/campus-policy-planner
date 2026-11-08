"""M3 门禁的端到端验收脚本（真实 HTTP，不打桩）。

为什么要有它：pytest 用的是 Flask test client + 临时库，证明的是"代码逻辑对"；
这个脚本起一个**真的 waitress 服务**、连 .env 指向的真库，证明的是"装起来真能跑"。
两者失败原因完全不同（前者查逻辑，后者查环境/路径/编码），所以不能互相替代。

用法：
    python tools/e2e_gate.py              # 全套验收
    python tools/e2e_gate.py --keep-db    # 保留上传的演示文档，便于手工看界面
    python tools/e2e_gate.py --port 5066  # 换端口（默认 5066，避开开发常用 5000）

退出码：0 全过；1 有 FAIL。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "demo"

# 每个演示文件期望的结果：(文件名, 期望 HTTP, 期望业务码, 期望文档状态, 期望提示关键词)
EXPECT = [
    ("01-本科生奖学金评审办法（演示样例）.pdf", 201, 0, "indexed", None),
    ("02-学生事务办理指南（演示样例）.docx", 201, 0, "indexed", None),
    ("03-扫描件样例（演示样例）.pdf", 201, 0, "empty", "扫描件"),
    ("04-空文本样例（演示样例）.txt", 201, 0, "empty", "有效阈值"),
    # 扩展名说是 PDF，内容却是 MZ 头（Windows 可执行文件）→ 415 / 1005，在写盘之前就拦住
    ("05-改名的假文件（演示样例）.pdf", 415, 1005, None, None),
]


class _Abort(Exception):
    """中途无法继续（如登录失败）：跳出但保留已收集的结果，最后统一打印。"""


def wait_health(base: str, timeout: float = 30.0) -> dict:
    import httpx

    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            resp = httpx.get(f"{base}/api/v1/health", timeout=2.0)
            if resp.status_code == 200:
                return resp.json()
            last = f"HTTP {resp.status_code}"
        except Exception as exc:  # 服务还没起来
            last = type(exc).__name__
        time.sleep(0.4)
    raise SystemExit(f"服务在 {timeout}s 内没有就绪（最后状态：{last}）")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5066)
    parser.add_argument("--keep-db", action="store_true",
                        help="保留临时库（默认跑完即删），便于进去翻数据")
    args = parser.parse_args()

    import tempfile

    import httpx

    python = sys.executable
    # 用一次性临时库，而不是 .env 指向的 data/app.db：
    # 否则第二次跑会撞上 SHA-256 去重（同名同内容的文件返回已存在），验收结果不可重复。
    workdir = Path(tempfile.mkdtemp(prefix="campus-e2e-"))
    env = dict(os.environ)
    env["PORT"] = str(args.port)
    env["DATABASE_URL"] = f"sqlite:///{(workdir / 'app.db').as_posix()}"
    env["UPLOAD_DIR"] = str(workdir / "uploads")
    print(f"[0/5] 临时库：{workdir}")

    for tool in ("init_db.py", "seed_demo.py"):
        prep = subprocess.run([python, f"tools/{tool}"], cwd=str(ROOT), env=env,
                              capture_output=True, text=True)
        if prep.returncode != 0:
            print(f"准备失败：tools/{tool}\n{prep.stdout}\n{prep.stderr}")
            return 1

    print(f"[1/5] 启动服务 :{args.port} …")
    proc = subprocess.Popen(
        [python, "server.py", "--port", str(args.port)],
        cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    base = f"http://127.0.0.1:{args.port}"
    results: list[tuple[str, bool, str]] = []

    try:
        health = wait_health(base)
        data = health["data"]
        ok = health["code"] == 0 and data["db"] == "ok"
        results.append(("GET /health", ok,
                        f"code={health['code']} db={data['db']} version={data['version']} "
                        f"llm_mode={data['llm_mode']} docs={data['document_count']} chunks={data['chunk_count']}"))

        print("[2/5] 登录取令牌 …")
        resp = httpx.post(f"{base}/api/v1/auth/login",
                          json={"username": "admin", "password": "admin123"}, timeout=10)
        token = (resp.json().get("data") or {}).get("token")
        results.append(("POST /auth/login", resp.status_code == 200 and bool(token),
                        f"HTTP {resp.status_code} token={'有' if token else '无'}"))
        if not token:
            results.append(("后续步骤", False, "登录失败：请确认已跑 python tools/seed_demo.py（admin/admin123）"))
            raise _Abort()
        headers = {"Authorization": f"Bearer {token}"}

        print("[3/5] 逐个上传演示文件 …")
        uploaded: list[dict] = []
        for name, want_http, want_code, want_status, want_hint in EXPECT:
            path = DEMO / name
            if not path.exists():
                results.append((f"上传 {name}", False, "演示文件不存在，先跑 tools/make_demo_docs.py"))
                continue
            with open(path, "rb") as handle:
                resp = httpx.post(f"{base}/api/v1/documents", headers=headers,
                                  files={"file": (name, handle, "application/octet-stream")},
                                  data={"title": f"（演示样例）{name}"},
                                  timeout=120)
            body = resp.json()
            payload = body.get("data") or {}
            hint = payload.get("parse_hint") or ""
            status = payload.get("parse_status")
            detail = f"HTTP {resp.status_code} code={body.get('code')} status={status} hint={hint or '—'}"
            if payload.get("parse_error"):
                detail += f" error={payload['parse_error']}"

            ok = resp.status_code == want_http and body.get("code") == want_code
            if ok and want_status:
                ok = status == want_status
            if ok and want_hint:
                ok = want_hint in hint
            if resp.status_code == 201:
                uploaded.append(payload)
            results.append((f"上传 {name}", ok, detail))

        print("[4/5] 查看切分结果 …")
        indexed = [u for u in uploaded if u.get("parse_status") == "indexed"]
        if indexed:
            doc_id = indexed[0]["id"]
            resp = httpx.get(f"{base}/api/v1/documents/{doc_id}/chunks",
                             headers=headers, timeout=10)
            chunks = (resp.json().get("data") or {}).get("items") or []
            pages = sorted({c.get("page_number") for c in chunks if c.get("page_number")})
            results.append(("GET /documents/{id}/chunks", resp.status_code == 200 and len(chunks) > 0,
                            f"HTTP {resp.status_code} 片段数={len(chunks)} 覆盖页码={pages}"))
            lengths = [len(c["content"]) for c in chunks]
            results.append(("片段长度分布合理", all(20 <= n <= 1200 for n in lengths),
                            f"min={min(lengths)} max={max(lengths)}"))
        else:
            results.append(("GET /documents/{id}/chunks", False, "没有 indexed 状态的文档，无法验证切分"))

        print("[5/5] 重建索引并试检索 …")
        probe = subprocess.run([python, "tools/rebuild_index.py", "--probe", "家庭经济困难认定需要什么材料"],
                               cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120)
        ok = probe.returncode == 0 and "score=" in probe.stdout
        # 只抽 Top-1 那一行，避免把整段片段正文灌进汇总表
        top1 = next((ln.strip() for ln in probe.stdout.splitlines() if ln.strip().startswith("1. score=")), "")
        if top1:
            tail = [top1[:110]]
        else:
            tail = (probe.stdout or probe.stderr).strip().splitlines()[-1:] or ["(无输出)"]
        results.append(("tools/rebuild_index.py --probe", ok, " | ".join(tail)))

    except _Abort:
        pass
    except Exception as exc:
        results.append(("脚本自身异常", False, f"{type(exc).__name__}: {exc}"))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    # 汇总必须在清理临时库之前打印——否则 --keep-db 之外的情况看不到结果
    print("\n" + "=" * 78)
    width = max(len(name) for name, _, _ in results) + 2
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL':4}  {name:<{width}}{detail}")
    failed = sum(1 for _, ok, _ in results if not ok)
    print("=" * 78)
    print(f"合计 {len(results)} 项，通过 {len(results) - failed} 项，失败 {failed} 项")

    if args.keep_db:
        print(f"\n临时库保留在：{workdir}")
    else:
        import shutil

        shutil.rmtree(workdir, ignore_errors=True)
        print("\n临时库已清理。")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
