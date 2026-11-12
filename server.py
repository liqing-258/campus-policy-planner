"""生产 / 演示启动入口。

开发期用 `flask --app app run --debug`（热重载）；
演示、自检与打包后的 exe 都走本文件（waitress，Windows 上比 gunicorn 稳）。

    .venv\\Scripts\\python.exe server.py                 # 127.0.0.1:5000，自动打开浏览器
    .venv\\Scripts\\python.exe server.py --no-browser    # 只起服务（CI / 调试）
    .venv\\Scripts\\python.exe server.py --port 5001
    .venv\\Scripts\\python.exe server.py --bootstrap     # 建表 + 演示账号 + 导入示例政策
    .venv\\Scripts\\python.exe server.py --selftest      # 起服务、自检、退出（打包后验证用）

打包形态（PyInstaller 单文件）下，双击 exe 走 `main()`：没有参数时
自动 bootstrap → 起服务 → 打开浏览器，并把异常留在屏幕上，避免"一闪而过"。
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
import traceback
import webbrowser


def _open_browser_later(url: str, delay: float = 1.2) -> None:
    def run() -> None:
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:  # 没有默认浏览器不该让服务起不来
            pass

    threading.Thread(target=run, daemon=True).start()


def _bootstrap(app) -> None:
    from app import bootstrap

    with app.app_context():
        bootstrap.ensure_ready()


def _selftest(app, url: str) -> int:
    """打包后自检：真打一次 HTTP，确认真能服务，而不是"进程起来了"。"""
    import httpx

    checks: list[tuple[str, bool, str]] = []
    try:
        client = httpx.Client(timeout=15.0, trust_env=False)  # 本机代理会劫持 127.0.0.1
        health = client.get(f"{url}/api/v1/health")
        payload = health.json().get("data", {})
        checks.append(("GET /api/v1/health", health.status_code == 200, str(health.status_code)))
        checks.append(("数据库可用", payload.get("db") == "ok", str(payload.get("db"))))
        checks.append(("政策文件 > 0", payload.get("document_count", 0) > 0,
                       str(payload.get("document_count"))))
        index = client.get(f"{url}/")
        checks.append(("GET / 返回页面", index.status_code == 200 and "<div id=\"app\"" in index.text,
                       f"{index.status_code} / {len(index.text)}B"))
        login = client.post(f"{url}/api/v1/auth/login",
                            json={"username": "student", "password": "student123"})
        checks.append(("演示账号可登录", login.status_code == 200,
                       str(login.json().get("code"))))
        tasks = client.get(f"{url}/api/v1/tasks/stats")
        checks.append(("未授权访问被拒", tasks.status_code == 401, str(tasks.status_code)))
    except Exception as exc:
        checks.append(("自检请求异常", False, f"{type(exc).__name__}: {exc}"))

    print("\n[自检] 结果", flush=True)
    for name, ok, detail in checks:
        print(f"  {'[OK]  ' if ok else '[FAIL]'} {name:<24} {detail}", flush=True)
    failed = sum(1 for _, ok, _ in checks if not ok)
    print(f"[自检] {'全部通过' if failed == 0 else f'{failed} 项失败'}\n", flush=True)
    return 0 if failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动知校服务")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--debug", action="store_true", help="使用 Flask 内置服务器并开启调试")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--bootstrap", action="store_true", help="建表 + 演示账号 + 导入示例政策")
    parser.add_argument("--reset", action="store_true",
                        help="把现有数据库改名备份（不删除），再重新自举一份干净的演示数据")
    parser.add_argument("--selftest", action="store_true", help="起服务自检后退出")
    args = parser.parse_args(argv)

    from app import create_app
    from app.paths import is_frozen, writable_root

    frozen = is_frozen()

    if args.reset:
        # 改名而不是删除：用户点"重置"往往只是想回到演示状态，不该真的丢数据
        from datetime import datetime

        from app.config import settings as boot_settings

        db_path = boot_settings.sqlite_path
        if db_path and db_path.exists():
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = db_path.with_name(f"{db_path.name}.bak-{stamp}")
            db_path.rename(backup)
            print(f"[知校] 原数据库已改名备份：{backup.name}", flush=True)
        args.bootstrap = True

    app = create_app()
    settings = app.extensions["settings"]
    host = args.host or settings.host
    port = args.port or settings.port

    # 打包后数据落在 exe 同级 data/：把这一点明确打出来，用户才知道文件在哪
    if frozen or args.bootstrap:
        print(f"[知校] 数据目录 {writable_root() / 'data'}", flush=True)
        _bootstrap(app)

    if args.debug:
        app.run(host=host, port=port, debug=True)
        return 0

    from waitress import serve

    display_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    url = f"http://{display_host}:{port}"
    print(f"[知校] 服务启动于 {url}", flush=True)
    print(f"[知校] 健康检查 {url}/api/v1/health", flush=True)
    print(f"[知校] 问答模式 {settings.llm_mode} · 数据库 {settings.database_url}", flush=True)
    if not frozen and not args.no_browser and not args.selftest:
        print("[知校] 按 Ctrl+C 停止服务\n", flush=True)

    if args.selftest:
        thread = threading.Thread(
            target=lambda: serve(app, host=host, port=port, threads=4),
            daemon=True,
        )
        thread.start()
        time.sleep(1.5)
        return _selftest(app, url)

    if not args.no_browser and not frozen:
        _open_browser_later(url)
    elif frozen and not args.no_browser:
        _open_browser_later(url)

    serve(app, host=host, port=port, threads=8)
    return 0


def _pause_if_frozen() -> None:
    if getattr(sys, "frozen", False):
        try:
            input("按回车键关闭窗口…")
        except Exception:
            pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception:
        # 双击 exe 时错误必须留在屏幕上，否则窗口一闪而过，没法排查
        traceback.print_exc()
        _pause_if_frozen()
        sys.exit(1)
