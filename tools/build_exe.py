"""把「知校」打包成 Windows 单文件 exe，并铺好交付目录。

用法：
    .venv\\Scripts\\python.exe tools\\build_exe.py
    .venv\\Scripts\\python.exe tools\\build_exe.py --no-copy-data   # 不复制现有演示库

产物（dist/）：
    知校校园政策助手.exe      单文件，双击即用
    data\\                     数据库与上传件（**exe 同级**，不是临时解压目录）
    启动_知校.bat             起服务并打开浏览器
    启动_知校_不打开浏览器.bat  给浏览器没弹出来 / 想自己开的情况
    重置演示数据.bat          旧库改名备份后重建一份干净演示数据
    查看_访问地址.bat         打印地址与健康检查
    使用说明.txt              给拿到 exe 的人看

三条硬约束（都踩过）：
1. **资源与数据分开**：前端 dist / demo 打进包内（只读），data/ 落在 exe 同级（可写）。
2. **构建全程只新建文件**：工作目录带时间戳，不用 --clean，不覆盖已有产物目录。
3. **bat 用 GBK + CRLF**：中文 Windows 的 cmd 按 GBK 解释批处理，写 UTF-8 会乱码。
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
BUILD = ROOT / ".build"
NAME = "知校校园政策助手"

EXCLUDES = (
    "numpy", "pandas", "matplotlib", "PIL", "scipy", "tkinter",
    "IPython", "pytest", "notebook", "PyQt5", "PySide2",
)


def pkg_modules(pkg: str) -> list[str]:
    """扫目录生成模块清单，避免手写 hidden-import 漂移。"""
    base = ROOT / pkg
    mods = [pkg]
    mods += [f"{pkg}.{p.stem}" for p in sorted(base.glob("*.py")) if p.stem != "__init__"]
    return mods


def build() -> Path:
    stamp = time.strftime("%H%M%S")
    work = BUILD / f"{NAME}_{stamp}"
    out = work / "out"
    out.mkdir(parents=True, exist_ok=True)

    frontend = ROOT / "frontend" / "dist"
    if not (frontend / "index.html").is_file():
        sys.exit("前端未构建：先运行 cd frontend && npm run build")

    hidden: list[str] = []
    hidden += pkg_modules("app")
    hidden += pkg_modules("shared")
    hidden += ["waitress", "waitress.server", "waitress.channel",
               "waitress.task", "waitress.wasyncore",
               "sqlalchemy.dialects.sqlite", "sqlite3",
               "jieba", "httpx", "httpx._transports.default",
               "pydantic", "pydantic_core", "dotenv"]

    cmd = [sys.executable, "-m", "PyInstaller",
           "--noconfirm", "--onefile", "--console",
           "--name", NAME,
           "--distpath", str(out),
           "--workpath", str(work / "work"),
           "--specpath", str(work),
           "--paths", str(ROOT),
           "--add-data", f"{frontend}{';'}frontend/dist",
           "--add-data", f"{ROOT / 'demo'}{';'}demo",
           ]
    for mod in hidden:
        cmd += ["--hidden-import", mod]
    for mod in EXCLUDES:
        cmd += ["--exclude-module", mod]
    cmd.append(str(ROOT / "packaging" / "entry_zx.py"))

    print("[构建] PyInstaller 开始，首次约 2–4 分钟…", flush=True)
    started = time.time()
    result = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
    if result.returncode != 0:
        tail = "\n".join((result.stdout + result.stderr).splitlines()[-25:])
        sys.exit(f"[构建] 失败（退出码 {result.returncode}）\n{tail}")

    exe = out / f"{NAME}.exe"
    if not exe.is_file():
        sys.exit(f"[构建] 未找到产物 {exe}")

    DIST.mkdir(parents=True, exist_ok=True)
    target = DIST / exe.name
    shutil.copyfile(exe, target)          # 复制=写入，不触发删除阈值
    print(f"[构建] 完成，用时 {time.time() - started:.0f}s → {target}", flush=True)
    size = target.stat().st_size / 1024 / 1024
    sha = hashlib.sha256(target.read_bytes()).hexdigest()
    print(f"[构建] 体积 {size:.1f} MB · sha256 {sha[:16]}…", flush=True)
    return target


def write_bat(path: Path, lines: list[str]) -> None:
    path.write_text("\r\n".join(lines) + "\r\n", encoding="gbk", newline="")


def prepare_delivery(exe: Path, copy_data: bool) -> None:
    stamp = time.strftime("%Y%m%d-%H%M%S")

    write_bat(DIST / "启动_知校.bat", [
        "@echo off",
        "chcp 936 >nul",
        'cd /d "%~dp0"',
        f"title {NAME}",
        "echo ============================================================",
        "echo   知校 · 校园政策理解与智能事务规划系统",
        "echo   启动后会自动打开浏览器；关掉本窗口即停止服务",
        "echo ============================================================",
        "echo.",
        f'"{NAME}.exe"',
    ])

    write_bat(DIST / "启动_知校_不打开浏览器.bat", [
        "@echo off",
        "chcp 936 >nul",
        'cd /d "%~dp0"',
        f"title {NAME} - 不开浏览器",
        "echo ============================================================",
        "echo   只启动服务，不自动打开浏览器",
        "echo   请手动访问：http://127.0.0.1:5000",
        "echo ============================================================",
        "echo.",
        f'"{NAME}.exe" --no-browser',
    ])

    write_bat(DIST / "重置演示数据.bat", [
        "@echo off",
        "chcp 936 >nul",
        'cd /d "%~dp0"',
        f"title {NAME} - 重置演示数据",
        "echo 现有数据库会被**改名备份**（不是删除），然后重建一份干净的演示数据。",
        "echo 按任意键继续，Ctrl+C 取消。",
        "pause >nul",
        "echo.",
        f'"{NAME}.exe" --reset --no-browser',
    ])

    write_bat(DIST / "查看_访问地址.bat", [
        "@echo off",
        "chcp 936 >nul",
        'cd /d "%~dp0"',
        f"title {NAME} - 访问地址",
        "echo 本机访问：http://127.0.0.1:5000",
        "echo 健康检查：http://127.0.0.1:5000/api/v1/health",
        "echo.",
        "echo 演示账号：student / student123（学生）、admin / admin123（管理员）",
        "echo 数据目录：%~dp0data",
        "echo.",
        "pause",
    ])

    (DIST / "使用说明.txt").write_text(
        f"""知校 · 校园政策理解与智能事务规划系统（v0.7.0）

一、怎么启动
   双击「启动_知校.bat」，等 3–5 秒浏览器自动打开 http://127.0.0.1:5000
   （若没有自动打开，双击「启动_知校_不打开浏览器.bat」后手动访问上面的地址）

二、演示账号
   student / student123   学生：提问、资格匹配、办事务
   admin   / admin123     管理员：维护政策、复核规则

三、数据在哪
   data\\app.db —— 关掉程序数据不会丢，就在 exe 旁边
   想回到出厂演示状态：双击「重置演示数据.bat」（旧库会改名备份，不删除）

四、关于模型
   问答需要 .env 里的模型密钥。把 .env 放在 exe 同级即可；
   没有密钥时程序**不会报错**，而是进入降级模式：只返回检索到的政策原文，
   不做归纳——界面顶部会常驻一行说明。

五、可选：局域网访问
   "知校校园政策助手.exe" --host 0.0.0.0 --no-browser
   然后同一网段的设备访问 http://<本机IP>:5000

构建时间 {stamp}
""",
        encoding="utf-8",
        newline="\n",
    )

    if copy_data:
        src_db = ROOT / "data" / "app.db"
        dst_dir = DIST / "data"
        dst_dir.mkdir(parents=True, exist_ok=True)
        if src_db.is_file():
            shutil.copyfile(src_db, dst_dir / "app.db")
            print(f"[交付] 已复制演示库 {src_db.stat().st_size / 1024:.0f} KB", flush=True)
        src_up = ROOT / "data" / "uploads"
        if src_up.is_dir():
            dst_up = dst_dir / "uploads"
            dst_up.mkdir(parents=True, exist_ok=True)
            n = 0
            for item in src_up.iterdir():
                if item.is_file():
                    shutil.copyfile(item, dst_up / item.name)
                    n += 1
            print(f"[交付] 已复制上传件 {n} 个", flush=True)

    print(f"[交付] 目录 {DIST}", flush=True)
    for item in sorted(DIST.iterdir()):
        kind = "DIR " if item.is_dir() else "FILE"
        size = "" if item.is_dir() else f"{item.stat().st_size / 1024 / 1024:.1f}MB"
        print(f"        {kind} {item.name:<28} {size}", flush=True)

    # 不自动复制 .env：那等于把模型密钥塞进一个"准备发出去"的目录。
    # 要用真实模型就手动复制一次，并知道自己在做什么。
    print("\n[提示] 未复制 .env（避免把模型密钥打进可分发目录）。", flush=True)
    print("       想用真实模型：copy .env dist\\.env   ← 仅本机自用，不要把这个目录发给别人", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="打包 exe 并铺交付目录")
    parser.add_argument("--no-copy-data", action="store_true", help="不复制现有演示库")
    args = parser.parse_args()

    exe = build()
    prepare_delivery(exe, copy_data=not args.no_copy_data)
    print("\n[下一步] 验证：.venv\\Scripts\\python.exe tools\\verify_exe.py", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
