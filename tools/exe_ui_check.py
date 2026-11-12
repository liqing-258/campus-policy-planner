"""对着**打包好的 exe** 跑一次界面巡检：证明发出去的那份里，页面是能用的。

为什么需要它：
`tools/verify_exe.py` 的场景 C 调的是接口。接口能答，**不等于**用户打开页面能看见东西——
前端产物（`frontend/dist`）是构建时塞进 exe 的。如果 dist 比源码旧，页面就会是空的，
而接口测试一个字都不会提。R2 那次「文件（0）」正是这一层的事故，
当时的 224 项测试 + 7 页巡检（跑的是开发服务）全绿。

做法：把 `dist/` 复制到系统临时目录（**不在交付目录里跑**，否则数据会落进 dist/data），
用 exe 自己起服务（`--port` 挑空闲端口），再让 `browser_check.py` 打这个端口。

用法：
    .venv\\Scripts\\python.exe tools\\exe_ui_check.py
    .venv\\Scripts\\python.exe tools\\exe_ui_check.py --keep-shots .workbuddy\\shots-exe
"""

from __future__ import annotations

import argparse
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
NAME = "知校校园政策助手"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"


def free_port(start: int = 5111, tries: int = 20) -> int:
    for port in range(start, start + tries):
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise SystemExit(f"{start}–{start + tries} 都占着，找不到空闲端口")


def wait_health(base: str, timeout: float = 90.0) -> bool:
    """轮询而不是 sleep：单文件 exe 冷启动要解包，快慢能差好几倍。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base}/api/v1/health", timeout=3) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(0.8)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="对着 exe 跑界面巡检")
    parser.add_argument("--keep-shots", default=".workbuddy/shots-exe",
                        help="截图输出目录（相对仓库根）")
    args = parser.parse_args()

    exe_src = DIST / f"{NAME}.exe"
    if not exe_src.is_file():
        print(f"未找到 {exe_src}，先运行 tools/build_exe.py")
        return 1
    if not (DIST / "data" / "app.db").is_file():
        print("dist/data/app.db 不存在——交付目录缺少演示库，巡检会看到空页面")
        return 1

    box = Path(tempfile.mkdtemp(prefix="zx_exe_ui_"))
    shutil.copy2(exe_src, box / exe_src.name)
    shutil.copytree(DIST / "data", box / "data")
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    shots = (ROOT / args.keep_shots).resolve()
    print(f"[界面] 沙箱 {box}")
    print(f"[界面] 服务 {base}")

    log = (box / "run.log").open("wb")
    proc = subprocess.Popen(
        [str(box / exe_src.name), "--port", str(port), "--no-browser"],
        cwd=str(box), stdout=log, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    code = 1
    try:
        if not wait_health(base):
            print("[界面] exe 没起来，看沙箱里的 run.log")
            return 1
        code = subprocess.call(
            [str(PYTHON), str(ROOT / "tools" / "browser_check.py"), base, "--out", str(shots)]
        )
    finally:
        # 单文件 exe 是父子两段式，必须连子进程一起杀（见 verify_exe 的坑 1）
        subprocess.call(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log.close()
        time.sleep(0.8)
        shutil.rmtree(box, ignore_errors=True)

    print(f"[界面] browser_check 退出码 {code}（0 = 全过）；截图在 {shots}")
    return code


if __name__ == "__main__":
    sys.exit(main())
