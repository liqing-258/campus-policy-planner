"""打包入口（PyInstaller）。

与 `python server.py` 走同一条启动逻辑，只多两件事：

1. 源码方式运行时把项目根补进 `sys.path`（PyInstaller 模式不需要）
2. **显式 import 所有业务包**——`create_app()` 里的蓝图是在函数内 import 的，
   静态分析通常能看见，但"通常"在交付物上不够用。这里写死一遍，
   多花 0 秒构建时间，换掉"双击报 ModuleNotFoundError"这类只能靠用户发现的事故。
"""

from __future__ import annotations

import os
import sys

if not getattr(sys, "frozen", False):
    _ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)

# 显式导入（顺序无所谓，作用是让 PyInstaller 的依赖图一定包含它们）
import app  # noqa: E402,F401
import app.auth  # noqa: E402,F401
import app.bootstrap  # noqa: E402,F401
import app.chunking  # noqa: E402,F401
import app.config  # noqa: E402,F401
import app.contract  # noqa: E402,F401
import app.documents  # noqa: E402,F401
import app.errors  # noqa: E402,F401
import app.health  # noqa: E402,F401
import app.indexing  # noqa: E402,F401
import app.injection  # noqa: E402,F401
import app.intent  # noqa: E402,F401
import app.llm  # noqa: E402,F401
import app.matching  # noqa: E402,F401
import app.models  # noqa: E402,F401
import app.parsing  # noqa: E402,F401
import app.paths  # noqa: E402,F401
import app.plans  # noqa: E402,F401
import app.qa  # noqa: E402,F401
import app.retrieval  # noqa: E402,F401
import app.rules  # noqa: E402,F401
import app.runtime  # noqa: E402,F401
import app.schemas  # noqa: E402,F401
import app.tasks  # noqa: E402,F401
import app.versions  # noqa: E402,F401
import app.workflows  # noqa: E402,F401
import shared.codes  # noqa: E402,F401
import shared.enums  # noqa: E402,F401

from server import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
