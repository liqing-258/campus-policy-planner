"""路径解析：把"只读资源"和"可写数据"分开。

**为什么必须分**：PyInstaller 单文件 exe 运行时会把自身解压到 `%TEMP%\\_MEIPASS`，
退出即删除。如果数据库按 `Path(__file__).parent.parent` 算，就会落进那个临时目录，
用户一关程序，数据全没了——而开发时用源码跑完全看不出问题。

| 类别 | 打包后位置 | 用途 |
| --- | --- | --- |
| 只读资源 | `sys._MEIPASS` | 前端 dist、demo 示例文件、模板 |
| 可写数据 | exe 所在目录 | `data/app.db`、上传的附件、日志 |

源码运行时两者都等于项目根目录，因此开发与打包共用同一套代码。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_root() -> Path:
    """只读资源根目录。"""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", None) or Path(sys.executable).parent)
    return Path(__file__).resolve().parent.parent


def writable_root() -> Path:
    """可写根目录。打包后是 exe 所在目录，源码运行时是项目根目录。"""
    if is_frozen():
        return Path(os.path.dirname(os.path.abspath(sys.executable)))
    return Path(__file__).resolve().parent.parent


def frontend_dist() -> Path | None:
    """前端构建产物目录；不存在则返回 None（此时根路径退化为 JSON 说明）。"""
    candidate = resource_root() / "frontend" / "dist"
    return candidate if (candidate / "index.html").is_file() else None


def demo_dir() -> Path | None:
    candidate = resource_root() / "demo"
    return candidate if candidate.is_dir() else None
