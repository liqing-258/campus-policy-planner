"""把开发库里的演示文档刷新成 `demo/` 下最新的一批。

**为什么需要单独一个脚本，而不是直接删库重播**

删库重播最干净，但会连带毁掉两样东西：

1. `policy_rules` 里的 10 条**已复核**规则（M5 的"AI 起草、人定稿"就是靠它们证明的）；
2. 挂在规则上的 `match_sessions` / `action_plans` / `tasks` 溯源链。

规则表通过 `chunk_id` 外键绑在片段上，重解析会换 chunk_id —— 规则不是"失效"，
是直接指向不存在的行。所以有规则的文件**原地更新**（改元数据 + 替换片段文本），
没有规则的文件才删了重建。

**做四件事**

1. 备份 `data/app.db`（改名，不删）；
2. `demo/` 下有、库里没有的文件 → 导入（解析 → 切分 → 索引）；
3. 库里与 `demo/` 对得上的文件（按「规范化标题 + 年份」匹配）→ 更新标题、文件名、
   哈希，覆盖 `uploads/` 里的物理文件，并把片段与规则引文里的旧标记替换掉；
4. 库里有、`demo/` 里已经没有的文件（上一版的异常样本）→ 软删除，退出检索。

用法：
    .venv\\Scripts\\python.exe tools\\refresh_demo_library.py --dry-run
    .venv\\Scripts\\python.exe tools\\refresh_demo_library.py
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from app.contract import new_ulid  # noqa: E402
from app.documents import _parse_and_index  # noqa: E402
from app.models import Document, DocumentChunk, PolicyRule, User, db  # noqa: E402
from app.runtime import current_settings  # noqa: E402
from app.versions import normalize_title, year_of  # noqa: E402

DEMO_DIR = ROOT / "demo"
EXT_TYPE = {".pdf": "pdf", ".docx": "docx", ".txt": "txt", ".md": "md"}

# 文件名里的编号前缀（`01-`）只是排序用，不进标题
_PREFIX = re.compile(r"^\d+[-_\s]*")
# 上一版留下的标记：标题、文件名、片段正文、规则引文里都有
_STALE_MARK = "（演示样例）"

# 版本元数据：只有这两份文件能确定，其余不猜（留空 = 未标注，比编一个"现行有效"好）
VERSION_META = {
    ("本科生奖学金评审办法", 2026): {
        "academic_year": "2026-2027",
        "published_at": "2026-09-01",
        "effective_status": "active",
    },
    ("本科生奖学金评审办法", 2025): {
        "academic_year": "2025-2026",
        "published_at": "2025-09-01",
        "effective_status": "superseded",
    },
}


def title_of(path: Path) -> str:
    return _PREFIX.sub("", path.stem)[:200]


def key_of(title: str, academic_year: str | None) -> tuple[str, int | None]:
    """判同一份文件的双键：规范化标题 + 年份。

    只比标题不行——`（2026年版）` 与 `（2025年版）` 规范化后是同一个串，
    两份会互相覆盖；只比文件名也不行——上一版的文件名里带着「（演示样例）」。

    这里比 `app.versions.normalize_title` 多做一步：去掉开头的编号。
    因为旧文档的 `title` 是当年手填的 `01-本科生奖学金评审办法（2025年版）`，
    编号被 normalize 当普通字符保留了下来，而新文档的 title 已被
    `bootstrap._title_of` 去掉编号——不补这一步，两份永远匹配不上。
    只删 1–2 位且后面紧跟非数字，`2024年…` 这种年份不会被误伤。
    """
    topic = re.sub(r"^\d{1,2}(?=\D)", "", normalize_title(title))
    return topic, year_of(title, academic_year)


def backup_database(dry_run: bool) -> Path | None:
    database = db.engine.url.database
    if not database:
        return None
    source = Path(database)
    if not source.exists():
        return None
    target = source.with_name(f"{source.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
    if dry_run:
        print(f"[dry] 将备份 {source.name} → {target.name}")
        return target
    shutil.copy2(source, target)
    print(f"[ok] 备份 {source.name} → {target.name}（{target.stat().st_size // 1024} KB）")
    return target


def scrub_text(value: str | None) -> str | None:
    if value is None or _STALE_MARK not in value:
        return value
    return value.replace(_STALE_MARK, "")


def prune_uploads(upload_dir: Path, dry_run: bool) -> None:
    """删掉 `uploads/` 里不再被任何文档引用的物理文件。

    为什么会有孤儿：重建分支每次都给新文档起一个新的 `stored_name`，
    旧文档行删了，它那份物理文件却留在磁盘上。跑几轮刷新之后，
    `uploads/` 里的文件数就是"库内引用数 × 轮数"——交付目录里一堆用不到的文件。

    **默认不删**，要显式 `--prune-uploads`。理由：删文件是不可逆动作，
    而"多留几个文件"最坏只是占点磁盘。只在两种情况下删：
    名字在库里查不到，**且**文件名符合本项目的存储命名（ULID + 扩展名），
    避免误删有人手工放进来的说明文件。
    """
    referenced = {
        row[0] for row in db.session.execute(db.select(Document.stored_name)).all()
    }
    pattern = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}\.(pdf|docx|txt|md)$")
    orphans = []
    for path in upload_dir.iterdir():
        if not path.is_file() or path.name in referenced:
            continue
        if not pattern.match(path.name):
            continue
        orphans.append(path)

    if not orphans:
        print("[prune] uploads/ 没有孤儿文件")
        return
    total = sum(p.stat().st_size for p in orphans)
    if dry_run:
        print(f"[dry] 将删除 {len(orphans)} 个孤儿文件（{total // 1024} KB）")
        return
    for path in orphans:
        path.unlink()
    print(f"[prune] 删除 {len(orphans)} 个孤儿文件（{total // 1024} KB），"
          f"保留 {len(referenced)} 份在用文件")


def main() -> int:
    parser = argparse.ArgumentParser(description="刷新开发库里的演示文档")
    parser.add_argument("--dry-run", action="store_true", help="只看会做什么，不写库")
    parser.add_argument("--prune-uploads", action="store_true",
                        help="顺带清理 uploads/ 里不再被引用的孤儿文件（默认不删）")
    args = parser.parse_args()

    files = sorted(
        p for p in DEMO_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in EXT_TYPE
    )
    if not files:
        raise SystemExit("demo/ 下没有可导入的文件，先跑 tools/make_demo_docs.py")

    app = create_app()
    with app.app_context():
        settings = current_settings()
        backup_database(args.dry_run)

        admin = db.session.execute(
            db.select(User).where(User.role == "admin")
        ).scalar_one_or_none()
        if admin is None:
            raise SystemExit("库里没有管理员账号，先跑 tools/seed_demo.py")

        active_docs = list(db.session.execute(
            db.select(Document).where(Document.deleted_at.is_(None))
        ).scalars())
        by_key: dict[tuple[str, int | None], Document] = {}
        for doc in active_docs:
            by_key.setdefault(key_of(doc.title, doc.academic_year), doc)

        wanted: set[str] = set()
        stats = {"added": 0, "updated": 0, "kept": 0, "dropped": 0}

        for path in files:
            content = path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            title = title_of(path)
            key = key_of(title, None)
            doc = by_key.get(key)
            wanted.add(key)

            if doc is None:
                if args.dry_run:
                    print(f"[dry] 新增 {path.name}")
                    stats["added"] += 1
                    continue
                ext = path.suffix.lower().lstrip(".")
                stored_name = f"{new_ulid()}.{ext}"
                (settings.upload_dir / stored_name).write_bytes(content)
                doc = Document(
                    id=new_ulid(),
                    title=title,
                    file_name=path.name[:255],
                    stored_name=stored_name,
                    file_type=EXT_TYPE[path.suffix.lower()],
                    file_size=len(content),
                    file_hash=digest,
                    parse_status="uploaded",
                    uploader_id=admin.id,
                )
                db.session.add(doc)
                db.session.commit()
                _parse_and_index(doc, settings)
                print(f"[new] {title}  → {doc.parse_status} / {doc.chunk_count} 片段")
                stats["added"] += 1
                continue

            # 已存在：有已复核规则就不动片段结构，只换文本与元数据
            approved = db.session.execute(
                db.select(db.func.count()).select_from(PolicyRule)
                .where(PolicyRule.document_id == doc.id)
                .where(PolicyRule.review_status == "approved")
            ).scalar_one()

            if args.dry_run:
                mode = "原地更新（有 %d 条已复核规则）" % approved if approved else "重建（无规则）"
                print(f"[dry] {doc.title} → {title}：{mode}")
                stats["updated"] += 1
                continue

            if approved:
                doc.title = title
                doc.file_name = path.name[:255]
                doc.file_size = len(content)
                doc.file_hash = digest
                (settings.upload_dir / doc.stored_name).write_bytes(content)
                chunks = db.session.execute(
                    db.select(DocumentChunk).where(DocumentChunk.document_id == doc.id)
                ).scalars()
                touched = 0
                for chunk in chunks:
                    cleaned = scrub_text(chunk.content)
                    if cleaned != chunk.content:
                        chunk.content = cleaned
                        touched += 1
                rules = db.session.execute(
                    db.select(PolicyRule).where(PolicyRule.document_id == doc.id)
                ).scalars()
                for rule in rules:
                    rule.original_quote = scrub_text(rule.original_quote) or rule.original_quote
                print(f"[upd] {title}（保留 {approved} 条规则，清洗 {touched} 个片段）")
            else:
                # 没有规则依赖 → 删了重建，保证片段与新文件逐字一致。
                # 必须先清片段再删文档：`document_chunks.document_id` 是外键，
                # SQLite 开了外键约束，直接删 documents 会 IntegrityError。
                db.session.execute(
                    db.delete(PolicyRule).where(PolicyRule.document_id == doc.id)
                )
                db.session.execute(
                    db.delete(DocumentChunk).where(DocumentChunk.document_id == doc.id)
                )
                db.session.delete(doc)
                db.session.commit()
                ext = path.suffix.lower().lstrip(".")
                stored_name = f"{new_ulid()}.{ext}"
                (settings.upload_dir / stored_name).write_bytes(content)
                fresh = Document(
                    id=new_ulid(),
                    title=title,
                    file_name=path.name[:255],
                    stored_name=stored_name,
                    file_type=EXT_TYPE[path.suffix.lower()],
                    file_size=len(content),
                    file_hash=digest,
                    parse_status="uploaded",
                    uploader_id=admin.id,
                )
                db.session.add(fresh)
                db.session.commit()
                _parse_and_index(fresh, settings)
                print(f"[reb] {title} → {fresh.chunk_count} 片段")

            meta = VERSION_META.get(key)
            if meta:
                # 重建分支换了 id，统一按标题取回当前有效的那一份
                current = db.session.execute(
                    db.select(Document).where(
                        Document.title == title, Document.deleted_at.is_(None)
                    )
                ).scalar_one_or_none()
                if current is not None:
                    current.academic_year = meta["academic_year"]
                    current.published_at = datetime.fromisoformat(meta["published_at"])
                    current.effective_status = meta["effective_status"]
                    db.session.commit()
                    print(f"      版本元数据：{meta['academic_year']} / "
                          f"{meta['published_at']} / {meta['effective_status']}")
            stats["updated"] += 1

        # 库里还有、demo/ 里已经没有 → 软删除
        for doc in active_docs:
            key = key_of(doc.title, doc.academic_year)
            if key in wanted:
                continue
            if args.dry_run:
                print(f"[dry] 软删除 {doc.title}")
                stats["dropped"] += 1
                continue
            doc.deleted_at = datetime.now(timezone.utc).replace(tzinfo=None)
            db.session.commit()
            print(f"[del] 软删除 {doc.title}（退出检索，历史引用保留）")
            stats["dropped"] += 1

        if not args.dry_run:
            from app.indexing import rebuild_index
            info = rebuild_index()
            print(f"[idx] 索引重建：{info}")

        if args.prune_uploads:
            prune_uploads(settings.upload_dir, args.dry_run)

        print("\n汇总：" + " · ".join(f"{k}={v}" for k, v in stats.items()))
        if args.dry_run:
            print("（dry-run，未写库）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
