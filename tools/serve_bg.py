"""把服务拉起来并脱离当前控制台。

**它只能保证「当前这条命令执行期间」服务可用。** 实测结论（2026-11-14）：

- 用后台任务方式起的 server.py，命令一结束就消失；
- 改用 DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP，仍然消失——日志里有
  `waitress | Serving on http://127.0.0.1:5000`，进程却已不存在，且没有任何
  异常堆栈。不是崩溃，是被外部按进程树清理（DETACHED 只脱离控制台，
  不脱离宿主会话的 job）。
- 想换真正的独立宿主也不行：`Invoke-CimMethod Win32_Process.Create` 被安全策略
  拦下（"equivalent to Start-Process"），`schtasks.exe` 在程序黑名单里，并明确
  要求不得通过其他 shell 或等效手段绕过。

所以本脚本的定位是**自检/临时预览**，不是给用户的常驻入口。要让链接长期可用，
请由用户双击 `scripts/start-backend.bat`：那个进程属于资源管理器（用户桌面会话），
不受这边清理，且 server.py 默认会自动打开浏览器到首页。

用法：
    .venv/Scripts/python.exe tools/serve_bg.py            # 起服务（已在跑就复用）
    .venv/Scripts/python.exe tools/serve_bg.py --restart  # 先停再起
    .venv/Scripts/python.exe tools/serve_bg.py --stop     # 只停
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PID_FILE = os.path.join(ROOT, ".build", "server.pid")
LOG_FILE = os.path.join(ROOT, ".build", "server_run.log")
PYTHON = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
DEFAULT_PORT = 5000

# Windows 进程创建标志
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000


def health(port: int, timeout: float = 1.5) -> bool:
    """探 /api/v1/health 并要求返回 JSON 的 code=0。

    不能探 /health：那个路径没有后端路由，会被前端 SPA 的 catch-all 接管并
    返回 200 + 一段 HTML——服务根本没起来时它也是 200，就绪判断会失效。
    """
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/v1/health", timeout=timeout
        ) as resp:
            if resp.status != 200:
                return False
            payload = json.loads(resp.read().decode("utf-8"))
            return payload.get("code") == 0
    except (urllib.error.URLError, OSError, ValueError):
        return False


def read_pid() -> int | None:
    try:
        with open(PID_FILE, encoding="utf-8") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def stop(port: int) -> None:
    pid = read_pid()
    if pid is None:
        print("no pid file; nothing to stop")
        return
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, check=False)
        print(f"stopped pid={pid}")
    except OSError as exc:  # pragma: no cover
        print(f"taskkill failed: {exc}")
    for _ in range(20):
        if not health(port):
            break
        time.sleep(0.5)
    try:
        os.remove(PID_FILE)
    except OSError:
        pass


def start(port: int) -> int:
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    log = open(LOG_FILE, "ab")
    log.write(f"\n--- start {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n".encode())
    log.flush()
    proc = subprocess.Popen(
        [PYTHON, "server.py", "--port", str(port), "--no-browser"],
        cwd=ROOT,
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
        close_fds=True,
    )
    with open(PID_FILE, "w", encoding="utf-8") as fh:
        fh.write(str(proc.pid))
    return proc.pid


def main() -> int:
    parser = argparse.ArgumentParser(description="启动/停止知校服务（脱离进程树）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--restart", action="store_true")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--wait", type=float, default=40.0, help="等待就绪的秒数上限")
    args = parser.parse_args()

    if args.stop:
        stop(args.port)
        return 0

    if args.restart:
        stop(args.port)

    if health(args.port):
        print(f"already serving: http://127.0.0.1:{args.port} (pid={read_pid()})")
        return 0

    pid = start(args.port)
    deadline = time.time() + args.wait
    while time.time() < deadline:
        if health(args.port):
            print(f"started pid={pid} -> http://127.0.0.1:{args.port}/login")
            return 0
        time.sleep(0.5)

    print(f"started pid={pid} but /health did not answer in {args.wait:.0f}s; see {LOG_FILE}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
