"""政策版本冲突检测（T-12）。

对应设计说明书 §11.2 T-12：
> 两版政策冲突 → 并列展示 + 日期差异提示

**这个问题为什么拖到 M6 才做**

不是因为难，是因为**没有数据可判**。判定"这两份是同一政策的两个版本"需要三个信号：
标题、年份/发布日期、有效状态。而 M3 建表时 `academic_year`、`published_at`、
`effective_status` 三列虽然都在，上传接口也支持写入，**但前端一直没有入口**，
演示库里 4 份文件这三个字段全是 NULL。在这种数据上写检测逻辑，
只能造一批假数据来跑测试——那证明不了任何事。

M6 做两件事把它变成真的：

1. 前端上传表单补上「学年 / 发布日期 / 有效状态」三个字段（后端从 M3 起就支持）；
2. 补演示数据：一份 2025 年版奖学金办法（截止日期与 2026 版不同），
   让"两版并列 + 日期差异"能被真实复现，而不是只在单测里成立。

**判同主题为什么只比标题，不做语义聚类**

语义聚类（向量、关键词重合）会产生"看起来相关"的误报：奖学金办法与勤工助学办法
都含"家庭经济困难"，但它们不是同一政策的两个版本。**版本冲突提示一旦误报，
用户就会开始忽略它**，这比不提示更糟。所以这里取最保守的规则：
规范化标题完全相同，且年份（或发布日期）不同。

代价是漏报——标题改过名的两个版本查不出来。这个缺口写在
`docs/50-测试与评测/测试报告.md` 的已知局限里，不假装覆盖到了。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from app.models import Document, db
from shared.enums import EFFECTIVE_ACTIVE
from shared.enums import EFFECTIVE_ACTIVE

# 标题规范化时要去掉的东西。顺序有讲究：先去掉括号内容再去年份，
# 否则「（2025年修订）」里的年份会因为括号已删而漏掉——这里处理不了，
# 所以 `_year_of` 是在**原始标题**上找年份，不是规范化之后。
_PAREN = re.compile(r"[（(][^）)]{0,40}[）)]")
_YEAR = re.compile(r"(20\d{2})\s*年?")
_VERSION_WORDS = re.compile(
    r"(修订版|修订|试行版|试行|暂行|正式版|版本|征求意见稿|第[一二三四五六七八九十]+版|v\d+(?:\.\d+)*)",
    re.IGNORECASE,
)
_NOISE = re.compile(r"[^\w\u4e00-\u9fff]+")

# 规范化标题的最小长度：太短的标题（如"通知"）互相比较没有意义，
# 两份都叫《通知》的文件不该被判定成同一政策的两个版本。
MIN_TOPIC_LEN = 6


def normalize_title(title: str) -> str:
    """把「（演示样例）2026 年本科生奖学金评审办法（修订版）.pdf」压成主题串。

    去掉：括号内容、年份、版本词、扩展名、标点空格。
    保留：主题本身。宁可压得狠一点（多合并）也不要压得松（误报），
    因为后续还有年份差异这一道关卡，压狠了只会漏报，压松了会误报。
    """
    text = title or ""
    if "." in text:
        text = text.rsplit(".", 1)[0]  # 去掉 .pdf / .docx
    text = _PAREN.sub("", text)
    text = _YEAR.sub("", text)
    text = _VERSION_WORDS.sub("", text)
    text = _NOISE.sub("", text)
    return text


def year_of(title: str, academic_year: str | None) -> int | None:
    """年份信号：先看标题里写的年份，再看 `academic_year` 学年的起始年。

    「2025-2026 学年」取 2025：政策通常按学年发布，文件名与学年标签指的是同一件事，
    取起始年才能让"2025 版"和"2025-2026 学年"对齐。
    """
    for candidate in (title or "", academic_year or ""):
        match = _YEAR.search(candidate)
        if match:
            return int(match.group(1))
    return None


@dataclass
class _Version:
    document_id: str
    title: str
    year: int | None
    academic_year: str | None
    published_at: str | None
    effective_status: str
    version_label: str | None
    topic: str

    def public(self) -> dict:
        return {
            "document_id": self.document_id,
            "title": self.title,
            "year": self.year,
            "academic_year": self.academic_year,
            "published_at": self.published_at,
            "effective_status": self.effective_status,
            "version_label": self.version_label,
        }

    @property
    def sort_key(self) -> tuple:
        """越新的排越前：先看发布日期，没日期看年份，都没有排最后。"""
        return (
            self.published_at or "",
            f"{self.year or 0:04d}",
            self.document_id,
        )


def _load_versions(document_ids: list[str]) -> dict[str, _Version]:
    if not document_ids:
        return {}
    rows = db.session.execute(
        db.select(Document).where(Document.id.in_(document_ids))
    ).scalars().all()
    versions: dict[str, _Version] = {}
    for row in rows:
        versions[row.id] = _Version(
            document_id=row.id,
            title=row.title,
            year=year_of(row.title, row.academic_year),
            academic_year=row.academic_year,
            published_at=row.published_at.isoformat() if row.published_at else None,
            effective_status=row.effective_status or "unknown",
            version_label=row.version_label,
            topic=normalize_title(row.title),
        )
    return versions


def _day_gap(newer: _Version, older: _Version) -> int | None:
    if not newer.published_at or not older.published_at:
        return None
    try:
        a = date.fromisoformat(newer.published_at)
        b = date.fromisoformat(older.published_at)
    except ValueError:
        return None
    return abs((a - b).days)


def _hint(group: list[_Version], newest: _Version) -> str:
    years = sorted({v.year for v in group if v.year is not None})
    if len(years) >= 2:
        span = f"{years[0]} 与 {years[-1]} 两个年份"
    elif years:
        others = [v for v in group if v.year is None]
        span = (f"其中 {len(others)} 份未标注年份，另一份标注为 {years[0]} 年")
    else:
        span = "两份文件的发布日期不同"
    parts = [f"知识库中有 {len(group)} 份主题相同但版本不同的文件（{span}）"]
    older = [v for v in group if v.document_id != newest.document_id]
    gaps = [g for g in (_day_gap(newest, v) for v in older) if g is not None]
    if gaps:
        parts.append(f"发布日期最多相差 {max(gaps)} 天")
    active = [v for v in group if v.effective_status == "active"]
    if len(active) == 1:
        parts.append(f"当前标注为「现行有效」的是《{active[0].title}》")
    elif not active:
        parts.append("两份都未标注为「现行有效」，无法自动判断应以哪份为准")
    else:
        parts.append("多份都标注为「现行有效」，标注本身存在冲突，请人工核对")
    parts.append("请以上述较新且有效的一份为准；若条款互相冲突，以原文为准")
    return "；".join(parts) + "。"


def detect(candidates: list) -> list[dict]:
    """从检索候选里找出"同一政策的多个版本"。

    只在**本次检索到的候选**里判：如果某一版没被召回，就不提示。
    理由：提示的前提是"用户看到的答案可能受影响"，而没被召回的那份
    并没有进入答案。把库里所有版本都扫一遍是另一个功能（版本巡检），
    误报成本高，不做。
    """
    document_ids = sorted({c.document_id for c in candidates})
    versions = _load_versions(document_ids)
    if len(versions) < 2:
        return []

    groups: dict[str, list[_Version]] = {}
    for version in versions.values():
        if len(version.topic) < MIN_TOPIC_LEN:
            continue
        groups.setdefault(version.topic, []).append(version)

    conflicts: list[dict] = []
    for topic, group in groups.items():
        if len(group) < 2:
            continue
        years = {v.year for v in group if v.year is not None}
        dates = {v.published_at for v in group if v.published_at}
        # 年份不同，或都没有年份但发布日期不同 —— 才算版本差异。
        # 年份完全一致且日期也一致的两份文件更可能是重复上传，交给去重逻辑处理。
        if len(years) < 2 and len(dates) < 2:
            continue
        group.sort(key=lambda v: v.sort_key, reverse=True)
        newest = group[0]
        cited = [c for c in candidates if c.document_id in {v.document_id for v in group}]
        conflicts.append({
            "topic": topic,
            "documents": [v.public() for v in group],
            "newest_document_id": newest.document_id,
            "day_gap": max(
                (g for g in (_day_gap(newest, v) for v in group[1:]) if g is not None),
                default=None,
            ),
            "cited_indexes": sorted({c.index for c in cited}),
            "hint": _hint(group, newest),
        })
    return conflicts


# ============================================================ R15：版本组与相似候选
def brief(doc: Document) -> dict:
    """版本列表里用的精简序列化。字段少而全：打开详情走 /documents/{id}。"""
    return {
        "document_id": doc.id,
        "title": doc.title,
        "file_type": doc.file_type,
        "department": doc.department,
        "published_at": doc.published_at.isoformat() if doc.published_at else None,
        "academic_year": doc.academic_year,
        "version_label": doc.version_label,
        "effective_status": doc.effective_status or "unknown",
        "supersedes_id": doc.supersedes_id,
        "created_at": doc.created_at.isoformat() + "Z",
    }


def group_of(doc: Document) -> list[Document]:
    """同一政策的全部版本（含自己）：规范化标题相同且主题长度达标。

    **只比标题，不聚类**——理由与 `detect` 相同：误报的版本提示比没有更糟。
    与 `detect` 的差别是范围：`detect` 只在本次检索候选里判，这里是**全库**，
    因为详情页的"历史版本"必须把库里所有同主题版本都列出来。
    """
    topic = normalize_title(doc.title)
    if len(topic) < MIN_TOPIC_LEN:
        return []
    rows = db.session.execute(
        db.select(Document).where(Document.deleted_at.is_(None))
    ).scalars().all()
    group = [d for d in rows if normalize_title(d.title) == topic]
    group.sort(key=lambda d: _Version(
        document_id=d.id, title=d.title, year=year_of(d.title, d.academic_year),
        academic_year=d.academic_year,
        published_at=d.published_at.isoformat() if d.published_at else None,
        effective_status=d.effective_status or "unknown",
        version_label=d.version_label, topic=topic,
    ).sort_key, reverse=True)
    return group


def newest_of(group: list[Document]) -> Document | None:
    return group[0] if group else None


def _bigrams(text: str) -> set[str]:
    return {text[i:i + 2] for i in range(len(text) - 1)}


def similarity(a: str, b: str) -> float:
    """标题相似度：规范化主题的字符二元组 Jaccard。"""
    ba, bb = _bigrams(a), _bigrams(b)
    if not ba or not bb:
        return 0.0
    return len(ba & bb) / len(ba | bb)


# 弱信号阈值：0.55。两份完全无关的中文标题通常 < 0.2；同一政策改名（如加"试行"）
# 常见 0.5–0.8。这个阈值只决定"要不要提示管理员看一眼"，不自动建任何关联——
# 提示错了最多多点一次"无关"，建错了会让旧版凭空"已被替代"。
WEAK_THRESHOLD = 0.55


def candidates_for(title: str, exclude_id: str | None = None) -> dict:
    """上传预检：与 `title` 同主题（强信号）或高度相似（弱信号）的既有文件。

    强 = 规范化标题完全相同；弱 = 一方主题包含另一方（均 ≥ MIN_TOPIC_LEN）
    或相似度 ≥ WEAK_THRESHOLD。**两档分开返回**：管理员对强信号要认真核对，
    对弱信号扫一眼即可，混在一起会把强信号淹掉。
    """
    topic = normalize_title(title)
    rows = db.session.execute(
        db.select(Document).where(Document.deleted_at.is_(None))
    ).scalars().all()
    strong: list[dict] = []
    weak: list[dict] = []
    for doc in rows:
        if exclude_id and doc.id == exclude_id:
            continue
        other = normalize_title(doc.title)
        if len(topic) < MIN_TOPIC_LEN or len(other) < MIN_TOPIC_LEN:
            continue
        if topic == other:
            item = brief(doc)
            item["match"] = "same_topic"
            strong.append(item)
            continue
        contained = (topic in other or other in topic)
        sim = similarity(topic, other)
        if contained or sim >= WEAK_THRESHOLD:
            item = brief(doc)
            item["match"] = "similar"
            item["similarity"] = round(sim, 2)
            weak.append(item)
    strong.sort(key=lambda d: d["created_at"], reverse=True)
    weak.sort(key=lambda d: d.get("similarity", 0), reverse=True)
    return {"strong": strong, "weak": weak[:8]}


def related_versions(doc: Document) -> list[Document]:
    """完整版本家族：主题组 **并** 人工建立的 supersedes 链（两个方向）。

    为什么要并集：标题改过名的两个版本主题比对不出来，但管理员显式建过
    `supersedes_id` 关联——人工判断的可信度高于字符串比对，链上的一律算进来。
    """
    found: dict[str, Document] = {d.id: d for d in group_of(doc)}
    found[doc.id] = doc

    # 下游：我替代了谁，谁又替代了谁（沿 supersedes_id 往回走）
    cursor = doc.supersedes_id
    guard = 0
    while cursor and guard < 20:
        row = db.session.get(Document, cursor)
        if row is None or row.id in found:
            break
        found[row.id] = row
        cursor = row.supersedes_id
        guard += 1

    # 上游：谁替代了我（把全库 supersedes_id 指向我的接进来，再递归一层层收）
    rows = db.session.execute(
        db.select(Document).where(Document.deleted_at.is_(None))
    ).scalars().all()
    by_target: dict[str, list[Document]] = {}
    for row in rows:
        if row.supersedes_id:
            by_target.setdefault(row.supersedes_id, []).append(row)
    frontier = [doc.id]
    guard = 0
    while frontier and guard < 20:
        nxt: list[str] = []
        for target in frontier:
            for row in by_target.get(target, []):
                if row.id not in found:
                    found[row.id] = row
                    nxt.append(row.id)
        frontier = nxt
        guard += 1

    family = sorted(found.values(), key=lambda d: (
        d.published_at.isoformat() if d.published_at else "",
        d.created_at.isoformat() if d.created_at else "",
        d.id,
    ), reverse=True)
    return family


def replacer_of(doc: Document, family: list[Document] | None = None) -> Document | None:
    """谁替代了这一版：优先人工 supersedes 链，其次日期排序的头一个。

    **人工判断优先于日期推断**：测试里出现过"旧版标了已被替代，但另一份
    无发布日期的文件年份更大，于是横幅认为当前就是最新版"——
    日期只在没人明确说过话的时候才作数。
    """
    family = family if family is not None else related_versions(doc)
    return next((d for d in family if d.supersedes_id == doc.id), None) or (
        family[0] if family and family[0].id != doc.id else None
    )


def version_note_for(doc: Document) -> dict | None:
    """给匹配结果用的版本提醒：同主题/同链 ≥ 2 版才给，单版本返回 None。

    文案只说"存在多个版本，请核对"，**不判断哪版该用**——除非标注了唯一一份
    现行有效，否则连"应以哪版为准"都不说（见 `_hint` 同一原则）。
    """
    family = related_versions(doc)
    if len(family) < 2:
        return None
    newest = replacer_of(doc, family) or family[0]
    active = [d for d in family if d.effective_status == EFFECTIVE_ACTIVE]
    if len(active) == 1:
        hint = (f"知识库中有 {len(family)} 个版本；当前唯一标注「现行有效」的是"
                f"《{active[0].title}》，判定前请核对使用的是否为该版本。")
    else:
        hint = (f"知识库中有 {len(family)} 个版本且有效状态未唯一确定，"
                "请核对版本后再看判定结果，勿混用不同版本的条款。")
    return {
        "has_multiple": True,
        "count": len(family),
        "newest_document_id": newest.id,
        "versions": [brief(d) for d in family[:6]],
        "hint": hint,
    }
