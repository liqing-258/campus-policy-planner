"""陌生环境复现检查：从 `git archive HEAD` 解出来的干净树开始，按 README 走一遍。

M7 的门禁是"陌生环境 10 分钟复现成功"。这句话只有在**真的从零跑一遍并计时**之后才有意义，
所以这个脚本做四件事：

1. 只导出已提交的内容（`git archive`）——未提交的改动、.env、venv、node_modules 都不在里面，
   这与别人 clone 下来拿到的东西一致
2. 用全新 venv 装依赖、建库、灌演示数据
3. 装前端依赖并构建
4. 起服务自检（HTTP 真打一遍）

每一步单独计时，最后打印总耗时与"是否在 10 分钟内"。数字照实输出，超了就写超了。

用法：
    .venv\\Scripts\\python.exe tools/repro_check.py
    .venv\\Scripts\\python.exe tools/repro_check.py --keep   # 保留目录便于排查
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NODE = Path.home() / ".workbuddy" / "binaries" / "node" / "versions" / "22.22.2-2"
TARGET_SECONDS = 600


def run(label: str, cmd: list[str], cwd: Path, timeout: int = 900) -> tuple[bool, float, str]:
    started = time.time()
    print(f"\n[复现] {label}\n        $ {' '.join(cmd)}", flush=True)
    result = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                            errors="replace", timeout=timeout)
    elapsed = time.time() - started
    ok = result.returncode == 0
    tail = (result.stdout + result.stderr).strip().splitlines()[-3:]
    print(f"        {'OK ' if ok else 'FAIL'} {elapsed:5.1f}s  {' | '.join(tail)}", flush=True)
    return ok, elapsed, "\n".join(tail)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--dir", default=".workbuddy/repro")
    args = parser.parse_args()

    work = ROOT / args.dir
    src = work / "src"
    if work.exists():
        # 不删就换个名字，避免和本机的删除阈值较劲
        work = work.with_name(f"{work.name}-{time.strftime('%H%M%S')}")
        src = work / "src"
    src.mkdir(parents=True, exist_ok=True)

    steps: list[tuple[str, bool, float]] = []
    total_start = time.time()

    # 1) 只导出已提交的内容
    started = time.time()
    tar = subprocess.run(
        ["git", "archive", "--format=tar", "HEAD"],
        cwd=str(ROOT), capture_output=True,
    )
    if tar.returncode != 0:
        print("git archive 失败：", tar.stderr.decode("utf-8", "replace"))
        return 1
    import io
    import tarfile

    with tarfile.open(fileobj=io.BytesIO(tar.stdout)) as tf:
        tf.extractall(src)
    steps.append(("解包仓库（git archive HEAD）", True, time.time() - started))
    print(f"[复现] 干净树 {src}（{len(list(src.rglob('*')))} 个条目）", flush=True)

    # 2) 全新 venv
    venv = work / "venv"
    ok, sec, _ = run("建 venv", [sys.executable, "-m", "venv", str(venv)], src)
    steps.append(("建 venv", ok, sec))
    py = venv / "Scripts" / "python.exe"

    # 3) 装依赖
    ok, sec, _ = run("装后端依赖", [str(py), "-m", "pip", "install", "-q", "-r", "requirements.txt"], src)
    steps.append(("装后端依赖", ok, sec))

    # 4) 建库 + 演示数据
    for label, script in (
        ("建表", "tools/init_db.py"),
        ("写演示账号", "tools/seed_demo.py"),
        ("导入演示政策", "tools/seed_demo_versions.py"),
        ("生成演示计划与任务", "tools/seed_demo_flow.py"),
    ):
        extra: list[str] = []
        ok, sec, _ = run(label, [str(py), script, *extra], src)
        steps.append((label, ok, sec))

    # 5) 前端依赖 + 构建
    npm = NODE / "npm.cmd"
    node = NODE / "node.exe"
    env_cmd = [str(npm), "install", "--no-audit", "--no-fund"]
    ok, sec, _ = run("装前端依赖（npm install）", env_cmd, src / "frontend")
    steps.append(("装前端依赖（npm install）", ok, sec))

    ok, sec, _ = run("构建前端（vite build）",
                     [str(node), "./node_modules/vite/bin/vite.js", "build"],
                     src / "frontend")
    steps.append(("构建前端（vite build）", ok, sec))

    # 6) 起服务自检
    ok, sec, _ = run("起服务自检（--selftest）",
                     [str(py), "server.py", "--selftest", "--no-browser", "--port", "5077"],
                     src)
    steps.append(("起服务自检（--selftest）", ok, sec))

    total = time.time() - total_start
    print("\n[复现] 分步耗时")
    for label, ok, sec in steps:
        print(f"        {'OK  ' if ok else 'FAIL'} {sec:7.1f}s  {label}")
    print(f"        总计 {total:7.1f}s（门禁 {TARGET_SECONDS}s → "
          f"{'通过' if total <= TARGET_SECONDS else '超出'}）")

    failed = [label for label, ok, _ in steps if not ok]
    if failed:
        print("\n[复现] 失败步骤：" + "、".join(failed))
        print(f"[复现] 目录保留在 {src}（便于排查）")
        return 1

    # 顺带确认"没带 .env 也能起来"：应当是 degraded，而不是报错
    print("\n[复现] 注意：干净树里没有 .env，服务应以 degraded 模式启动（只回检索原文）。")
    print("        这正是 README 里承诺的默认行为，不是故障。")

    if args.keep:
        print(f"[复现] 目录保留在 {work}")
    else:
        print(f"[复现] 干净树保留在 {work}（未删除：下次运行会另建一个带时间戳的目录）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
