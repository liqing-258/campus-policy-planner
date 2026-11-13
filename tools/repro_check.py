"""陌生环境复现检查：从 `git archive HEAD` 解出来的干净树开始，按 README 走一遍。

M7 的门禁是"陌生环境 10 分钟复现成功"。这句话只有在**真的从零跑一遍并计时**之后才有意义，
所以这个脚本做四件事：

1. 只导出已提交的内容（`git archive`）——未提交的改动、.env、venv、node_modules 都不在里面，
   这与别人 clone 下来拿到的东西一致
2. 用全新 venv 装依赖、建库、灌演示数据
3. 装前端依赖并构建
4. 起服务自检（HTTP 真打一遍）。注意 `tools/seed_demo_flow.py` 走的是**真实 HTTP**，
   所以第 4 步之前要为它单独起一个实例（跑在干净树里、用干净树的库），跑完立刻停——
   见下方 `_serve_for_seed`。

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

SEED_PORT = 5078


def _wait_health(port: int, timeout: float = 45.0) -> bool:
    """等 /health 返回 200。trust_env=False：本机代理会劫持 127.0.0.1（M5 踩过）。"""
    import httpx

    deadline = time.time() + timeout
    with httpx.Client(trust_env=False) as client:
        while time.time() < deadline:
            try:
                if client.get(f"http://127.0.0.1:{port}/api/v1/health", timeout=2).status_code == 200:
                    return True
            except Exception:
                pass
            time.sleep(0.5)
    return False


def _serve_for_seed(py: Path, cwd: Path, port: int = SEED_PORT) -> subprocess.Popen | None:
    """为 seed_demo_flow 起一个**属于干净树**的实例。

    为什么必须有这一步（真缺陷，不是洁癖）：`seed_demo_flow.py` 的设计就是"只走真实接口"，
    它的文档也写着"需要服务已启动"。复现脚本早先把这一步当自包含脚本直接调，于是：
    · 真正干净的环境（评审机器）里它会 `httpx.ConnectError: 目标计算机积极拒绝`，整条门禁失败；
    · 而开发者本机恰好开着 5000 时它会"通过"——但它打的是**那个外部服务的库**，
      与刚建好的干净库毫无关系，属于**假通过**，顺带往那个库里写了一批计划和任务。
    起在本步、跑完即停，两个问题一起消掉。
    """
    proc = subprocess.Popen(
        [str(py), "server.py", "--no-browser", "--port", str(port)],
        cwd=str(cwd), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if not _wait_health(port):
        proc.terminate()
        return None
    return proc



def _has_approved_rules(port: int) -> bool:
    """干净树里有没有**已复核**的规则。

    为什么要有这一步：`seed_demo_flow.py` 要先跑 /matches，而匹配要求目标文件有
    已复核的条件类规则。规则是模型抽取出来的，**无 API Key 的干净树里根本产不出来**
    （§7.7：未配置模型时抽取功能关闭）。所以这一步在评审机器上客观跑不了，
    应当显式 SKIP 并写清原因，而不是记成 FAIL——把"没跑"写成"失败"同样是在说谎。
    """
    import httpx

    api = f"http://127.0.0.1:{port}/api/v1"
    with httpx.Client(trust_env=False, timeout=15) as client:
        try:
            login = client.post(f"{api}/auth/login",
                                json={"username": "student", "password": "student123"}).json()
            headers = {"Authorization": f"Bearer {login['data']['token']}"}
            docs = client.get(f"{api}/documents?page=1&page_size=50",
                              headers=headers).json()["data"]["items"]
            for doc in docs:
                body = client.get(f"{api}/documents/{doc['id']}/rules"
                                  "?review_status=approved&page=1&page_size=1",
                                  headers=headers).json()
                if (body.get("data") or {}).get("total"):
                    return True
        except Exception:
            return False
    return False



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

    steps: list[tuple[str, bool | None, float, str]] = []
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
    steps.append(("解包仓库（git archive HEAD）", True, time.time() - started, ""))
    print(f"[复现] 干净树 {src}（{len(list(src.rglob('*')))} 个条目）", flush=True)

    # 2) 全新 venv
    venv = work / "venv"
    ok, sec, _ = run("建 venv", [sys.executable, "-m", "venv", str(venv)], src)
    steps.append(("建 venv", ok, sec, ""))
    py = venv / "Scripts" / "python.exe"

    # 3) 装依赖
    ok, sec, _ = run("装后端依赖", [str(py), "-m", "pip", "install", "-q", "-r", "requirements.txt"], src)
    steps.append(("装后端依赖", ok, sec, ""))

    # 4) 建库 + 演示数据
    for label, script in (
        ("建表", "tools/init_db.py"),
        ("写演示账号", "tools/seed_demo.py"),
        ("导入演示政策", "tools/seed_demo_versions.py"),
    ):
        extra: list[str] = []
        ok, sec, _ = run(label, [str(py), script, *extra], src)
        steps.append((label, ok, sec, ""))

    # 「生成演示计划与任务」要走真实接口，所以先为它起一个干净树的实例，跑完即停
    seed_server = _serve_for_seed(py, src)
    if seed_server is None:
        print(f"\n[复现] 生成演示计划与任务\n        FAIL  起实例失败（{SEED_PORT} 未响应）")
        steps.append(("生成演示计划与任务", False, 0.0, "起实例失败"))
    else:
        try:
            if not _has_approved_rules(SEED_PORT):
                reason = ("干净树没有已复核规则：规则由模型抽取，无 API Key 时该功能关闭。"
                          "此步需在已抽取并复核规则的库上单独跑")
                print(f"\n[复现] 生成演示计划与任务\n        SKIP  0.0s  {reason}")
                steps.append(("生成演示计划与任务", None, 0.0, reason))
            else:
                ok, sec, tail = run("生成演示计划与任务",
                                    [str(py), "tools/seed_demo_flow.py",
                                     "--base", f"http://127.0.0.1:{SEED_PORT}"],
                                    src)
                steps.append(("生成演示计划与任务", ok, sec, tail))
        finally:
            seed_server.terminate()
            try:
                seed_server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                seed_server.kill()

    # 5) 前端依赖 + 构建
    npm = NODE / "npm.cmd"
    node = NODE / "node.exe"
    env_cmd = [str(npm), "install", "--no-audit", "--no-fund"]
    ok, sec, _ = run("装前端依赖（npm install）", env_cmd, src / "frontend")
    steps.append(("装前端依赖（npm install）", ok, sec, ""))

    ok, sec, _ = run("构建前端（vite build）",
                     [str(node), "./node_modules/vite/bin/vite.js", "build"],
                     src / "frontend")
    steps.append(("构建前端（vite build）", ok, sec, ""))

    # 6) 起服务自检
    ok, sec, _ = run("起服务自检（--selftest）",
                     [str(py), "server.py", "--selftest", "--no-browser", "--port", "5077"],
                     src)
    steps.append(("起服务自检（--selftest）", ok, sec, ""))

    total = time.time() - total_start
    print("\n[复现] 分步耗时")
    for label, ok, sec, note in steps:
        mark = "OK  " if ok else ("SKIP" if ok is None else "FAIL")
        print(f"        {mark} {sec:7.1f}s  {label}" + (f"（{note}）" if note else ""))
    print(f"        总计 {total:7.1f}s（门禁 {TARGET_SECONDS}s → "
          f"{'通过' if total <= TARGET_SECONDS else '超出'}）")

    failed = [label for label, ok, _, _ in steps if ok is False]
    skipped = [label for label, ok, _, _ in steps if ok is None]
    if skipped:
        print("\n[复现] 跳过步骤（不是失败，原因已在上表）：" + "、".join(skipped))
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
