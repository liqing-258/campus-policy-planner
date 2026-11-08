"""生产/演示启动入口。

开发期用 `flask --app app run --debug`（热重载），
演示与自检用本文件（waitress，Windows 上比 gunicorn 稳）。

    .venv\\Scripts\\python.exe server.py            # 默认 127.0.0.1:5000
    .venv\\Scripts\\python.exe server.py --port 5001
"""

from __future__ import annotations

import argparse

from app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="启动知校后端")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--debug", action="store_true", help="使用 Flask 内置服务器并开启调试")
    args = parser.parse_args()

    app = create_app()
    settings = app.extensions["settings"]
    host = args.host or settings.host
    port = args.port or settings.port

    if args.debug:
        app.run(host=host, port=port, debug=True)
        return

    from waitress import serve

    print(f"[知校] 服务启动于 http://{host}:{port}")
    print(f"[知校] 健康检查 http://{host}:{port}/api/v1/health")
    print(f"[知校] 模型模式 {settings.llm_mode} · 数据库 {settings.database_url}")
    serve(app, host=host, port=port, threads=8)


if __name__ == "__main__":
    main()
