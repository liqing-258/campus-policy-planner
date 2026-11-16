"""文档完整性检查（只读）：找出"被批量脚本写坏"的 Markdown。

**为什么需要这个工具**

这个项目的文档迭代靠一批 Python 脚本做 `sed` 式的插入与替换，实测出现过一次
把 3825 行的《系统设计说明书》写成 8 行（只剩标题）的事故——`git status` 当时显示为
普通的 `M`，一眼看不出是"整文件被清空"。那次是提交前顺手看了一眼 `git diff --stat`
才发现 `3817 deletions`。

所以这里把"看一眼"变成一条命令：**与 HEAD 逐文件比行数**，掉得多的直接报错。

用法：

    .venv\\Scripts\\python.exe tools\\doc_integrity.py          # 检查全部跟踪的 .md
    .venv\\Scripts\\python.exe tools\\doc_integrity.py --all    # 连 .txt / .json 一起查

判据（两条满足任一即报警）：
- 行数比 HEAD 少 20% 以上；
- 行数比 HEAD 少 50 行以上。

**只报"变小"**：变大是正常的（每轮都在补内容）。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_SUFFIXES = (".md",)
EXTRA_SUFFIXES = (".md", ".txt", ".json", ".ini", ".py", ".ts", ".vue")

SHRINK_RATIO = 0.80      # 掉到 80% 以下算可疑
SHRINK_LINES = 50        # 或绝对少 50 行以上


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} 失败：{result.stderr.strip()[:200]}")
    return result.stdout


def _lines(text: str) -> int:
    return text.count("\n") + (0 if text.endswith("\n") or not text else 1)


def main() -> int:
    parser = argparse.ArgumentParser(description="检查文档是否被批量脚本写坏")
    parser.add_argument("--all", action="store_true", help="连同 .txt/.json/.py 等一起查")
    parser.add_argument("--quiet", action="store_true", help="只打印结论")
    args = parser.parse_args()

    suffixes = EXTRA_SUFFIXES if args.all else DEFAULT_SUFFIXES
    tracked = [
        p for p in _git("ls-files").splitlines()
        if p.endswith(suffixes) and not p.startswith(".workbuddy/")
    ]

    suspicious: list[tuple[str, int, int]] = []
    checked = 0
    for rel in tracked:
        path = ROOT / rel
        if not path.exists():
            continue
        try:
            head_text = _git("show", f"HEAD:{rel}")
        except SystemExit:
            continue                      # 新文件，HEAD 里没有，跳过
        head_lines = _lines(head_text)
        if head_lines < SHRINK_LINES:     # 短文件掉了也正常（改一句话就能少一半）
            continue
        now_lines = _lines(path.read_text(encoding="utf-8", errors="replace"))
        checked += 1
        lost = head_lines - now_lines
        if lost >= SHRINK_LINES and now_lines < head_lines * SHRINK_RATIO:
            suspicious.append((rel, head_lines, now_lines))

    if suspicious:
        print(f"[FAIL] {len(suspicious)} 个文件的体积异常缩小（可能是批量脚本写坏了）：")
        for rel, before, after in suspicious:
            print(f"  - {rel}: {before} 行 → {after} 行")
        print()
        print("恢复办法：git checkout -- <文件>（确认丢失的内容已在 HEAD 里）")
        return 1

    if not args.quiet:
        print(f"[OK] 检查 {checked} 个文件，没有异常缩小")
    return 0


if __name__ == "__main__":
    sys.exit(main())
