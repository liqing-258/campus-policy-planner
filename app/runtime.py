"""运行时上下文访问。

工具脚本在应用上下文之外运行（tools/init_db.py），而请求内需要拿到
本次 app 的配置（测试会覆写），所以统一走 current_settings()：
有 app 上下文取 app 上的实例，否则退回进程级默认实例。
"""

from __future__ import annotations

from flask import current_app, has_app_context

from app.config import Settings, settings as _process_settings


def current_settings() -> Settings:
    if has_app_context():
        instance = current_app.extensions.get("settings")
        if isinstance(instance, Settings):
            return instance
    return _process_settings
