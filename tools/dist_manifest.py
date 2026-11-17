"""交付包完整性清单：给 `dist/` 里每个文件算一枚 SHA-256，并支持一键复核。

为什么要有它：交付包是**拷来拷去**的东西——U 盘、网盘、微信、邮件附件。
一个 109 MB 的 exe 在传输里被截断、或者某个 bat 被杀软改了两行，
现象都是"双击没反应"，而很难一眼看出是文件坏了还是环境问题。

有了清单，判断就变成一条命令：

    .venv\\Scripts\\python.exe tools\\dist_manifest.py            # 生成 / 刷新清单
    .venv\\Scripts\\python.exe tools\\dist_manifest.py --verify   # 复核，逐项对

清单文件写在 `dist/SHA256SUMS.txt`，格式与 `sha256sum` 兼容
（`<64 位十六进制><两个空格><相对路径>`），所以在 Linux/macOS 上也能直接
`sha256sum -c SHA256SUMS.txt`。

三条约定：

1. **清单自身不入清单**。否则"改清单"就能掩盖"改文件"。
   同理 **SQLite 的 `-wal` / `-shm` 也不入清单**：它们是运行期临时文件，跑一次程序就变，
   放进去只会制造假警报。
2. **路径用 `/` 分隔、排序输出**。Windows 上算出来的清单拿到别的机器也能对。
3. `--verify` 只读不写：缺文件、多文件、哈希不符三类分开报，退出码 1。

用法：
    .venv\\Scripts\\python.exe tools\\dist_manifest.py
    .venv\\Scripts\\python.exe tools\\dist_manifest.py --verify
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST_NAME = "SHA256SUMS.txt"
CHUNK = 1 << 20  # 1 MB：exe 有 109 MB，整读进内存没必要

# 运行期临时文件，**不进清单**：SQLite 一开 WAL 模式就会生成 `-wal` / `-shm`，
# 程序跑一次它们的哈希就变，放进清单会让复核永远报"多出 2 项 / 哈希不符"。
# 这与 `verify_exe` 里"比对时跳过 app.db-shm/wal"是同一条规矩。
TRANSIENT_SUFFIXES = ("-wal", "-shm", ".db-journal")


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(CHUNK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def collect(dist: Path) -> list[tuple[str, str, int]]:
    """返回 [(相对路径, sha256, 字节数)]，按路径排序，跳过清单自身。"""
    items: list[tuple[str, str, int]] = []
    for path in sorted(dist.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(dist).as_posix()
        if rel == MANIFEST_NAME:
            continue
        if path.name.endswith(TRANSIENT_SUFFIXES):
            continue
        items.append((rel, sha256_of(path), path.stat().st_size))
    return items


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", default=str(ROOT / "dist"))
    ap.add_argument("--verify", action="store_true", help="只读复核，不刷新清单")
    args = ap.parse_args()

    dist = Path(args.dist)
    if not dist.is_dir():
        print(f"[清单] 目录不存在：{dist}")
        return 1
    manifest = dist / MANIFEST_NAME

    if args.verify:
        if not manifest.exists():
            print(f"[清单] 没有清单文件：{manifest}（先不带 --verify 跑一次生成）")
            return 1
        recorded: dict[str, str] = {}
        for line in manifest.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            digest, _, rel = line.partition("  ")
            recorded[rel] = digest

        actual = {rel: digest for rel, digest, _ in collect(dist)}
        missing = sorted(set(recorded) - set(actual))
        extra = sorted(set(actual) - set(recorded))
        changed = sorted(rel for rel in set(recorded) & set(actual) if recorded[rel] != actual[rel])

        print(f"[清单] 清单记录 {len(recorded)} 项 / 实际 {len(actual)} 项")
        for label, rows in (("缺失", missing), ("多出", extra), ("哈希不符", changed)):
            if rows:
                print(f"  {label} {len(rows)} 项：")
                for rel in rows[:10]:
                    print(f"    {rel}")
                if len(rows) > 10:
                    print(f"    …另有 {len(rows) - 10} 项")
        if missing or extra or changed:
            print("[清单] 结论：**不一致**（交付包被动过或传坏了）")
            return 1
        print("[清单] 结论：一致（逐项哈希全部匹配）")
        return 0

    items = collect(dist)
    if not items:
        print(f"[清单] {dist} 里没有文件，未生成清单")
        return 1
    total = sum(size for _, _, size in items)
    lines = [
        "# 知校 · 交付包完整性清单（SHA-256）",
        "# 生成方式：.venv\\Scripts\\python.exe tools\\dist_manifest.py",
        "# 复核方式：.venv\\Scripts\\python.exe tools\\dist_manifest.py --verify",
        "# 清单自身不入清单——否则改清单就能掩盖改文件。",
        f"# 文件数 {len(items)} · 合计 {total / 1048576:.1f} MB",
        "",
    ]
    lines += [f"{digest}  {rel}" for rel, digest, _ in items]
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"[清单] 已写入 {manifest}")
    print(f"[清单] 文件 {len(items)} 个 · 合计 {total / 1048576:.1f} MB")
    for rel, _, size in sorted(items, key=lambda row: -row[2])[:3]:
        print(f"  最大：{rel}  {size / 1048576:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
