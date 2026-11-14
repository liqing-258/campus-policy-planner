"""管理员数据看板：学生画像记录与提问偏好统计。

## 这一层只做聚合，不造数据

两条接口读的都是**业务表里已经存在的事实**：

- 画像 ← `match_sessions`（学生在资格匹配页点「开始匹配」时落库的那一行，
  `profile_json` 是当时的完整画像快照）；不改表、不新增"画像"概念；
- 提问 ← `messages` 里 `role='user'` 的正文，配 `conversations` 找归属人。

所以"管理员看到的画像数"和学生端真做过的次数天然一致。反过来，如果这里另建
一张 `student_profiles` 表，就得靠代码去同步，早晚出现两套数字。

## 三个数字的口径

| 指标 | 算法 | 为什么是它 |
| --- | --- | --- |
| 平均符合率 | Σ符合 ÷ Σ(符合+不符合+无法判定) | 三者都算分母：无法判定也是"评估过的一条" |
| 画像完整度 | 已填字段数 ÷ 9（九个画像字段） | 只认非空；"任意"这类选项算已填 |
| 疑问条数 | `messages` 中 role=user 的行数 | 一条提问一行，追问也各自算一条 |

## 时区

库里的 `created_at` 是 **naive UTC**（`utcnow()`）。新增接口一律输出带 `Z` 的
ISO8601，浏览器 `new Date()` 才会转成本地时间；时段分布也先加 8 小时再分桶，
否则"晚上问得多"会被系统性错判成"下午问得多"。本系统面向国内高校，偏移量写死
`UTC+8`——真要支持多时区得从用户资料里取，那是另一件事。
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone

from flask import Blueprint

from app.auth import admin_required
from app.contract import ok
from app.models import Conversation, Document, MatchSession, Message, User, db, utcnow

bp = Blueprint("analytics", __name__)

# 单次聚合读取上限。课程演示的数据量在几十条量级，这个上限只是防"某天库被塞爆
# 之后管理页转圈十分钟"，并不改变口径——超限时 `truncated=true` 会如实告诉前端。
MAX_ROWS = 2000

LOCAL_UTC_OFFSET = 8

# 画像九个字段。顺序与 MatchView 的字段顺序一致，前端表格按这个顺序取。
PROFILE_FIELDS = (
    "student_type", "grade", "gpa", "rank_percent", "major",
    "credits_earned", "enrollment_status", "poverty_certified", "disciplinary_record",
)

# 提问主题词表。**刻意写成规则而不是让模型分类**：管理看板要的是"同一批数据
# 每次刷新给同样的图"，模型分类做不到这点，而且几十条问题不值得多花一次调用。
TOPIC_RULES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("scholarship", "奖助学金", ("奖学金", "助学金", "励志", "国家奖", "资助", "贫困", "补助", "助学贷款", "勤工")),
    ("honor", "评优评先", ("评优", "三好", "优秀", "标兵", "先进", "优秀学生", "十佳")),
    ("discipline", "违纪处分", ("处分", "违纪", "警告", "记过", "留校", "开除", "作弊", "旷课")),
    ("status", "学籍管理", ("学籍", "转专业", "休学", "复学", "退学", "毕业", "学位", "转学")),
    ("study", "课程学业", ("补考", "重修", "绩点", "学分", "缓考", "选课", "考试", "四级", "六级", "成绩")),
    ("living", "校园生活", ("宿舍", "住宿", "食堂", "后勤", "门禁", "校园卡", "图书馆", "社团")),
)

TOPIC_OTHER = ("other", "其他事务")

# 抽取关键词时要挡掉的疑问词与空壳词。jieba 的 TF-IDF 会把"什么""哪些"排得很高，
# 它们出现在词云里除了占地方没有任何信息量。
STOPWORDS = frozenset({
    "什么", "哪些", "怎么", "如何", "可以", "需要", "是否", "有没有", "多少",
    "请问", "一下", "这个", "那个", "我们", "他们", "自己", "以及", "还有",
    "规定", "要求", "相关", "有关", "情况", "问题", "办法", "政策", "文件",
    "学生", "学校", "的", "了", "吗", "呢", "吧", "和", "与", "或",
    # 下列是实际跑出来占位但零信息量的：疑问尾巴（几本/几点/多久/时候）、
    # 被 jieba 切碎的半截词（对绩 → "对绩点"）、版本口水（两版/年版）
    "时候", "多久", "几点", "几本", "几号", "几天", "对绩", "两版", "年版",
    "满足", "条件", "关键", "条款", "差别", "不一样", "一样", "一般", "具体",
    "怎么办", "怎样", "怎么弄", "可不可以",
})

_jieba_ready = False


def _iso_utc(value: datetime | None) -> str | None:
    """naive UTC → 带 Z 的 ISO8601。没有它，浏览器会把 UTC 当本地时间显示。"""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def _profile_of(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _filled(profile: dict) -> int:
    return sum(1 for key in PROFILE_FIELDS if str(profile.get(key) or "").strip() not in ("", "None"))


def _label(value: object, fallback: str = "未填写") -> str:
    text = str(value or "").strip()
    return text if text else fallback


def _normalize_student_type(raw: str) -> str:
    """把学生类型归到四类，供统计图使用。

    为什么需要归并：`profile_json` 存的是**前端当时提交的原值**，历史上选项换过
    几版（`full_time_undergraduate`、`全日制在校本科生（具有本校正式学籍）`……），
    直接 group by 会得到一张八瓣、每瓣 1 条的饼图，读不出任何趋势。

    归并只按关键词，**不猜语义**：专科 / 本科 / 研究生三个词都认不出来就归"其他"，
    原值另存在 `raw_student_types` 里，谁要核对都能对上。
    """
    text = (raw or "").strip()
    if not text or text == "None":
        return "未填写"
    lowered = text.lower()
    if "专科" in text or "vocational" in lowered:
        return "专科生"
    if "本科" in text or "undergraduate" in lowered or "bachelor" in lowered:
        return "本科生"
    if any(word in text for word in ("研究生", "硕士", "博士")) or "master" in lowered or "phd" in lowered:
        return "研究生"
    return "其他"


# ---------------------------------------------------------------- 画像记录
@bp.get("/admin/analytics/portraits")
@admin_required
def portraits():
    """学生画像记录：逐条明细 + 汇总统计。

    明细里**保留画像摘要**（学生类型/年级/绩点/专业），因为管理员要看的是
    "这个学生以什么条件被判定了什么"，只给一个 session 编号等于什么都没说。
    """
    sessions = db.session.execute(
        db.select(MatchSession).order_by(MatchSession.created_at.desc()).limit(MAX_ROWS)
    ).scalars().all()

    user_ids = {s.user_id for s in sessions}
    doc_ids = {s.document_id for s in sessions}
    users = {
        u.id: u for u in db.session.execute(
            db.select(User).where(User.id.in_(user_ids))
        ).scalars()
    } if user_ids else {}
    docs = {
        d.id: d for d in db.session.execute(
            db.select(Document).where(Document.id.in_(doc_ids))
        ).scalars()
    } if doc_ids else {}

    items = []
    type_counter: Counter[str] = Counter()
    raw_type_counter: Counter[str] = Counter()
    grade_counter: Counter[str] = Counter()
    completeness_total = 0
    met = not_met = unknown = 0
    recent_cutoff = utcnow() - timedelta(days=7)
    recent = 0

    for session in sessions:
        profile = _profile_of(session.profile_json)
        user = users.get(session.user_id)
        doc = docs.get(session.document_id)

        completeness_total += _filled(profile)
        raw_type = _label(profile.get("student_type"))
        type_counter[_normalize_student_type(raw_type)] += 1
        raw_type_counter[raw_type] += 1
        grade_counter[_label(profile.get("grade"))] += 1
        met += session.sum_met
        not_met += session.sum_not_met
        unknown += session.sum_unknown
        if session.created_at and session.created_at >= recent_cutoff:
            recent += 1

        items.append({
            "session_id": session.id,
            "student": user.display_name if user else "已删除用户",
            "student_no": user.username if user else "—",
            "document_title": doc.title if doc else "已删除文件",
            "document_id": session.document_id,
            "student_type": _label(profile.get("student_type")),
            "grade": _label(profile.get("grade")),
            "gpa": _label(profile.get("gpa"), "—"),
            "major": _label(profile.get("major"), "—"),
            "met": session.sum_met,
            "not_met": session.sum_not_met,
            "unknown": session.sum_unknown,
            "completeness": round(_filled(profile) / len(PROFILE_FIELDS), 3),
            "created_at": _iso_utc(session.created_at),
        })

    judged = met + not_met + unknown
    summary = {
        "session_count": len(sessions),
        "student_count": len(user_ids),
        "document_count": len(doc_ids),
        "recent_7d": recent,
        "met": met,
        "not_met": not_met,
        "unknown": unknown,
        "judged": judged,
        "met_rate": round(met / judged, 3) if judged else None,
        "unknown_rate": round(unknown / judged, 3) if judged else None,
        "avg_completeness": round(completeness_total / (len(sessions) * len(PROFILE_FIELDS)), 3)
        if sessions else None,
        "by_student_type": [
            {"label": label, "count": count}
            for label, count in type_counter.most_common()
        ],
        "raw_student_types": [
            {"label": label, "count": count}
            for label, count in raw_type_counter.most_common()
        ],
        "by_grade": [
            {"label": f"{label} 年级" if label.isdigit() else label, "count": count}
            for label, count in sorted(grade_counter.items(), key=lambda kv: (not kv[0].isdigit(), kv[0]))
        ],
        "truncated": len(sessions) >= MAX_ROWS,
        "generated_at": _iso_utc(utcnow()),
    }
    return ok({"summary": summary, "items": items})


# ---------------------------------------------------------------- 提问分析
def _ensure_jieba() -> None:
    global _jieba_ready
    if _jieba_ready:
        return
    import jieba  # noqa: F401  —— 首次 import 会建前缀词典，几百毫秒
    import jieba.analyse  # noqa: F401

    # jieba 默认把建词典的过程打到 DEBUG，管理页点一下就给控制台刷三行日志。
    # 它不是本项目的 logger，只能从它自己的入口压住。
    import logging

    jieba.setLogLevel(logging.INFO)

    _jieba_ready = True


def _keywords(texts: list[str], top_k: int = 36) -> list[dict]:
    """TF-IDF 关键词。用 jieba 的 `extract_tags`，再按词频补上 count。

    词云里字号由 weight 决定、悬停提示要显示"出现了几次"，两者不是一回事：
    weight 高但只出现一次的词会让提示显得莫名其妙，所以两个都给。
    """
    if not texts:
        return []
    _ensure_jieba()
    import jieba.analyse

    blob = "\n".join(texts)
    pairs = jieba.analyse.extract_tags(blob, topK=top_k * 4, withWeight=True)
    rows = []
    for word, weight in pairs:
        word = word.strip()
        if len(word) < 2 or word in STOPWORDS or word.isdigit():
            continue
        count = blob.count(word)
        # 只出现一次的词进词云等于噪声：字小到看不清，悬停还只能回一句"出现 1 次"。
        # 这条规则同时天然挡掉了 jieba 切出来的半截词（它们往往只被切到一次）。
        if count < 2:
            continue
        rows.append({"word": word, "weight": round(float(weight), 4), "count": count})
        if len(rows) >= top_k:
            break
    return rows


def _topic_of(text: str) -> tuple[str, str]:
    """按词表判主题。多个主题命中时取"命中词总长度最长"的那个——
    只数命中个数会让"考试"（2 字）压过"奖学金"（3 字），长词通常更具体。
    """
    best = (TOPIC_OTHER[0], TOPIC_OTHER[1])
    best_score = 0
    for key, label, words in TOPIC_RULES:
        score = sum(len(word) for word in words if word in text)
        if score > best_score:
            best, best_score = (key, label), score
    return best


@bp.get("/admin/analytics/qa")
@admin_required
def qa_overview():
    """提问分析：关键词词云数据 + 主题分布 + 时段分布 + 活跃提问人。"""
    rows = db.session.execute(
        db.select(Message, User, Conversation)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .join(User, User.id == Conversation.user_id)
        .where(Message.role == "user")
        .order_by(Message.created_at.desc())
        .limit(MAX_ROWS)
    ).all()

    texts: list[str] = []
    topic_counter: Counter[str] = Counter()
    topic_labels: dict[str, str] = {}
    topic_examples: dict[str, list[str]] = {}
    hour_counter: Counter[int] = Counter()
    asker_counter: Counter[str] = Counter()
    weekday_counter: Counter[str] = Counter()
    weekday_labels = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

    for message, user, _conv in rows:
        text = (message.content or "").strip()
        if not text:
            continue
        texts.append(text)
        key, label = _topic_of(text)
        topic_counter[key] += 1
        topic_labels[key] = label
        bucket = topic_examples.setdefault(key, [])
        if len(bucket) < 3:
            bucket.append(text[:60])
        asker_counter[user.display_name if user else "已删除用户"] += 1
        if message.created_at:
            local = message.created_at + timedelta(hours=LOCAL_UTC_OFFSET)
            hour_counter[local.hour] += 1
            weekday_counter[weekday_labels[local.weekday()]] += 1

    total = sum(topic_counter.values())
    topics = [
        {
            "key": key,
            "label": topic_labels.get(key, key),
            "count": count,
            "share": round(count / total, 3) if total else 0,
            "examples": topic_examples.get(key, []),
        }
        for key, count in topic_counter.most_common()
    ]

    # 24 小时补零：前端画图不需要自己补空档，也避免"缺的时段"被压成一条直线
    hours = [{"hour": h, "count": hour_counter.get(h, 0)} for h in range(24)]

    question_count = sum(asker_counter.values())
    summary = {
        "question_count": question_count,
        "asker_count": len(asker_counter),
        "conversation_count": len({c.id for _m, _u, c in rows}),
        "keyword_count": 0,
        "peak_hour": max(hours, key=lambda item: item["count"])["hour"] if question_count else None,
        "truncated": len(rows) >= MAX_ROWS,
        "generated_at": _iso_utc(utcnow()),
    }

    keywords = _keywords(texts)
    summary["keyword_count"] = len(keywords)

    return ok({
        "summary": summary,
        "keywords": keywords,
        "topics": topics,
        "hours": hours,
        "weekdays": [{"label": label, "count": weekday_counter.get(label, 0)} for label in weekday_labels],
        "top_askers": [
            {"name": name, "count": count} for name, count in asker_counter.most_common(6)
        ],
    })
