"""打包后自检：在**系统临时目录**里真跑一遍交付物，跑两个场景。

场景 A「裸 exe」：沙箱里只放 exe —— 验证首次运行能自己建表、写演示账号、导入示例政策。
场景 B「带库交付」：沙箱里放 exe + dist/data/ —— 验证真正要交出去的那一份能直接打开，
                  知识库里是 5 份政策 / 6 条任务，而不是空库。

为什么不在 dist/ 里跑：数据库按设计落在 exe 同级 `data/`。在交付目录里跑一次，
交付目录就会多出一份测试数据，拷给别人的那份就带着我的演示痕迹。所以复制到临时沙箱，
跑完删干净，并断言 dist/ 前后逐项一致。

三条踩过的坑（都在本文件里处理）：
1. 单文件 exe 是"启动器 + 子进程"两段式，只杀父进程会留下孤儿占住 exe 与数据库 → `taskkill /T`
2. `taskkill` 在中文 Windows 上输出 GBK，`text=True` 会在 reader 线程里抛 UnicodeDecodeError
3. 块缓冲 + 强杀会让重定向输出变成 0 字节 → 程序自己 flush，这里再按 UTF-8/GBK 双解码兜底
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
NAME = "知校校园政策助手"


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260),
    ]


def list_processes() -> list[tuple[int, str]]:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snap == wintypes.HANDLE(-1).value:
        return []
    rows: list[tuple[int, str]] = []
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    ok = k32.Process32FirstW(snap, ctypes.byref(entry))
    while ok:
        rows.append((entry.th32ProcessID, entry.szExeFile))
        ok = k32.Process32NextW(snap, ctypes.byref(entry))
    return rows


def _kill(pid: int) -> None:
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)


def kill_tree(pid: int, tag: str, sandbox: Path) -> None:
    _kill(pid)
    # 父进程先死时 taskkill /T 找不到子进程，按 exe 路径兜底再捞一遍
    for child, exe_path in list_processes():
        if str(sandbox).lower() in exe_path.lower() or tag in exe_path:
            _kill(child)


def rmtree_retry(path: Path, attempts: int = 8, delay: float = 0.6) -> bool:
    for _ in range(attempts):
        try:
            shutil.rmtree(path)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            for pid, exe in list_processes():
                if str(path).lower() in exe.lower():
                    _kill(pid)
            time.sleep(delay)
    try:
        shutil.rmtree(path)
        return True
    except Exception as exc:  # 不静默：留着几百 MB 的沙箱比报错更糟
        print(f"  ! 沙箱未删除，请手动清理：{path}（{exc}）")
        return False


def snapshot(folder: Path) -> dict[str, int]:
    return {
        str(p.relative_to(folder)): (p.stat().st_size if p.is_file() else -1)
        for p in sorted(folder.rglob("*"))
    }


def decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gbk", errors="replace")


def run_case(label: str, with_data: bool) -> tuple[str, int, Path]:
    sandbox = Path(tempfile.mkdtemp(prefix="zx_exe_verify_"))
    exe_src = DIST / f"{NAME}.exe"
    shutil.copy2(exe_src, sandbox / exe_src.name)

    if with_data:
        src = DIST / "data"
        if src.is_dir():
            shutil.copytree(src, sandbox / "data")

    log = sandbox / "selftest.log"
    print(f"\n[自检] 场景 {label} · 沙箱 {sandbox}")
    proc = None
    code = -1
    try:
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        with log.open("wb") as fh:
            proc = subprocess.Popen(
                [str(sandbox / exe_src.name), "--selftest", "--no-browser"],
                cwd=str(sandbox), stdout=fh, stderr=subprocess.STDOUT, env=env,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
            deadline = time.time() + 180
            while proc.poll() is None and time.time() < deadline:
                time.sleep(0.4)
            code = proc.poll() if proc.poll() is not None else -1
    finally:
        if proc is not None and proc.poll() is None:
            kill_tree(proc.pid, NAME, sandbox)
    time.sleep(0.6)

    raw = log.read_bytes() if log.exists() else b""
    text = decode(raw)
    print(f"[自检] {label} 输出（末 18 行）")
    for line in text.splitlines()[-18:]:
        print("   ", line)
    return text, code, sandbox


def check_common(text: str, code: int, label: str, failures: list[str]) -> None:
    if code != 0:
        failures.append(f"{label}：exe 退出码 {code}（应为 0）")
    for needle in ("[自检] 结果", "全部通过"):
        if needle not in text:
            failures.append(f"{label}：输出里没有「{needle}」")


def main() -> int:
    exe_src = DIST / f"{NAME}.exe"
    if not exe_src.is_file():
        print(f"未找到 {exe_src}，先运行 tools/build_exe.py")
        return 1

    before = snapshot(DIST)
    failures: list[str] = []

    # ---- 场景 A：裸 exe，首次运行自举 ----
    text, code, box = run_case("A 裸 exe", with_data=False)
    check_common(text, code, "场景 A", failures)
    db = box / "data" / "app.db"
    if not db.is_file():
        failures.append("场景 A：数据库没有落在 exe 同级 data/")
    uploads = box / "data" / "uploads"
    if not uploads.is_dir() or not any(uploads.iterdir()):
        failures.append("场景 A：示例政策没有导入 uploads/")
    if "建表 13 张" not in text:
        failures.append("场景 A：没有执行建表自举")
    listing = sorted(f"{p.name}:{p.stat().st_size // 1024}KB"
                     for p in (box / "data").iterdir()) if (box / "data").is_dir() else []
    print(f"[自检] 场景 A data/ → {listing}")
    rmtree_retry(box)

    # ---- 场景 B：exe + 交付库 ----
    if (DIST / "data" / "app.db").is_file():
        text_b, code_b, box_b = run_case("B 带库交付", with_data=True)
        check_common(text_b, code_b, "场景 B", failures)
        if "政策文件 > 0" not in text_b:
            failures.append("场景 B：知识库计数检查没跑")

        import sqlite3

        con = sqlite3.connect(f"file:{DIST / 'data' / 'app.db'}?mode=ro", uri=True)
        docs = con.execute("select count(*) from documents").fetchone()[0]
        chunks = con.execute("select count(*) from document_chunks").fetchone()[0]
        tasks = con.execute("select count(*) from tasks").fetchone()[0]
        con.close()
        print(f"[自检] 交付库 documents={docs} chunks={chunks} tasks={tasks}")
        if docs < 5 or chunks < 10 or tasks < 1:
            failures.append(f"场景 B：交付库内容不完整（{docs}/{chunks}/{tasks}）")
        rmtree_retry(box_b)
    else:
        failures.append("dist/data/app.db 不存在，交付目录缺少演示库")

    after = snapshot(DIST)
    if before != after:
        added = set(after) - set(before)
        changed = {k for k in before if k in after and before[k] != after[k]}
        failures.append(f"交付目录被污染：新增 {sorted(added)} 变更 {sorted(changed)}")
    else:
        print("\n[自检] 交付目录未被污染（前后逐项一致）")

    if failures:
        print("\n[自检] 失败项：")
        for item in failures:
            print("  -", item)
        return 1
    print("\n[自检] exe 两个场景均通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
