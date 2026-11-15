"""校园事务指南（R12）。

## 这一层不存政策，只存"指向"

需求写得很清楚：不要另建一套政策库，指南必须能追到真实文件。所以这里
**没有新表**，只有一份编目表（`GUIDE_ITEMS`）：每条事项记住三样东西——

1. `doc_key`：政策文件标题（去掉版本括注，如「本科生奖学金评审办法」）；
2. `sections`：条件 / 材料 / 流程 / 常见问题分别落在哪一章；
3. `articles`：需要更精确时，指向章内的哪一条（如「第四条」）。

请求到达时才去 `documents / document_chunks / policy_rules` 取正文。这样做的
三个直接后果，正好对上需求里的三条硬约束：

- **不重复**：正文一个字都不存库，指南与知识库永远同源；
- **会更新**：`doc_key` 解析时**优先取 `effective_status=active` 的版本**，
  新版本一上传并把旧版标成 `superseded`，指南自动指向新版，不需要重灌数据；
- **不编造**：正文是原文摘录（`blocks[].text` 逐字来自 `document_chunks`），
  文件缺失时返回 `missing_policy`，前端显示「暂未收录相关政策」。

## 与另外三个模块的关系

指南**不复制**它们的判断，只把入口接过去：

- 智能问答：给一条带事项名的问题（`actions.ask`），前端带 `?q=` 跳转；
- 资格匹配：给 `document_id`，并事先算好这份文件有没有**已复核的条件规则**
  （`actions.match.available`）——没规则时匹配接口会返回 2008，与其让用户跳过去
  撞一次，不如在指南里就说清"还不能匹配"；
- 计划生成：给 `document_id`，前端调既有的 `POST /plans/draft` 与
  `POST /plans/{id}/confirm`。草稿确认机制、任务状态机都在原来那套代码里，
  这里一行都没有重写。

## 编目为什么会"缺一条"

`GUIDE_ITEMS` 是人工编目，不是自动抽取。新上传一份政策不会被自动变成事项——
自动归类会把「违纪处分」这类不适合主动浏览的文件也塞进指南，且章节映射
很难自动判断。代价写在已知限制 C-19 里：新政策上线后，指南要等一次编目更新。
事项本身仍会**自动跟随版本**，这一点由 `_pick_document` 保证。
"""

from __future__ import annotations

import re

from flask import Blueprint, request

from app.auth import login_required
from app.contract import ok, page_args, paginated
from app.errors import GuideNotFound, InvalidParam
from app.models import Document, DocumentChunk, PolicyRule, db
from shared.enums import (
    CONDITION_RULE_TYPES,
    EFFECTIVE_ACTIVE,
    EFFECTIVE_SUPERSEDED,
    REVIEW_APPROVED,
    RULE_DEADLINE,
    RULE_MATERIAL,
    RULE_PROCESS,
)

bp = Blueprint("guides", __name__)

# ---------------------------------------------------------------- 分类
# id 与前端图标一一对应（frontend/src/views/GuideView.vue 的 ICONS）。
# `note` 是给用户看的覆盖说明——"这个分类里为什么只有升学类事项"必须写在明面上，
# 否则读者会以为系统漏了就业招聘政策。
CATEGORIES: list[dict] = [
    {
        "id": "scholarship",
        "label": "奖学金与助学金",
        "description": "奖助学金申请、助学贷款与家庭经济困难认定",
        "note": None,
    },
    {
        "id": "honors",
        "label": "评奖评优",
        "description": "学科竞赛获奖认定、创新创业项目与综合评优",
        "note": None,
    },
    {
        "id": "exams",
        "label": "考试与等级考试",
        "description": "四六级、计算机等级、普通话与职业资格",
        "note": None,
    },
    {
        "id": "academic",
        "label": "学籍与成绩",
        "description": "休学复学、转专业、成绩复核、缓考与学分认定",
        "note": None,
    },
    {
        "id": "campus-life",
        "label": "宿舍与校园生活",
        "description": "调宿退宿、校园卡、校园网、校医院与心理健康",
        "note": None,
    },
    {
        "id": "library",
        "label": "图书馆服务",
        "description": "借阅续借、自习与研讨空间、电子资源访问",
        "note": None,
    },
    {
        "id": "career",
        "label": "就业与实习",
        "description": "推免、考研支持与出国（境）交流",
        "note": "当前知识库收录的是升学与交流方向（推免、考研、出境交流）；就业招聘与实习类政策尚未收录。",
    },
    {
        "id": "service",
        "label": "请假与证明办理",
        "description": "请假销假、在读证明、成绩单与学籍异动受理",
        "note": None,
    },
]

_CATEGORY_BY_ID = {c["id"]: c for c in CATEGORIES}


# ---------------------------------------------------------------- 编目
def _item(
    key: str,
    category: str,
    title: str,
    doc_key: str,
    summary: str,
    *,
    sections: dict | None = None,
    articles: dict | None = None,
    lines: dict | None = None,
    keywords: tuple[str, ...] = (),
    recommended: bool = False,
    question: str | None = None,
) -> dict:
    """编目条目。`sections` / `articles` / `lines` 只写"需要精确指定"的字段。

    没写的字段走关键词自动匹配（见 `_FIELD_KEYWORDS`）：大多数文件里
    「申请条件 / 材料 / 流程 / 常见问题」四章标题都很规整，手写反而容易写错章节名。
    `lines` 是章内再筛一层（只留含关键词的行），用于「高频事项办理指引」这种
    一章写四件事的场合。
    """
    return {
        "key": key,
        "category": category,
        "title": title,
        "doc_key": doc_key,
        "summary": summary,
        "sections": sections or {},
        "articles": articles or {},
        "lines": lines or {},
        "keywords": keywords,
        "recommended": recommended,
        "question": question or f"{title}需要满足哪些条件、准备哪些材料、怎么办理？",
    }


GUIDE_ITEMS: list[dict] = [
    # ---------------------------------------------------------- 奖学金与助学金
    _item(
        "inspiring-scholarship", "scholarship", "国家励志奖学金申请",
        "本科生奖学金评审办法",
        "面向二年级以上（含二年级）家庭经济困难本科生，要求学年平均学分绩点不低于 3.0、"
        "专业排名前 30%，且同一学年未获国家奖学金。",
        sections={
            "conditions": "第二章 申请条件",
            "materials": "第四章 申请材料与附则",
            "steps": "第三章 评审程序与时间安排",
        },
        articles={"conditions": "第四条", "materials": "第九条"},
        keywords=("励志奖学金", "家庭经济困难", "绩点 3.0", "9 月 10 日"),
        recommended=True,
    ),
    _item(
        "excellent-scholarship", "scholarship", "校级优秀学生奖学金申请",
        "本科生奖学金评审办法",
        "面向全校本科生，要求学年平均学分绩点不低于 3.5、综合素质测评位于本专业前 15%，"
        "学年内无纪律处分记录。",
        sections={
            "conditions": "第二章 申请条件",
            "steps": "第三章 评审程序与时间安排",
        },
        articles={"conditions": "第五条"},
        keywords=("优秀学生奖学金", "综合测评", "绩点 3.5"),
    ),
    _item(
        "origin-student-loan", "scholarship", "生源地信用助学贷款",
        "国家助学贷款与勤工助学管理办法",
        "家庭经济困难全日制本专科学生可申请，每人每年最高 16000 元，在校期间利息由财政全额贴息；"
        "每年 7 月至 9 月在户籍所在地县级学生资助管理中心办理。",
        sections={
            "conditions": "第一章 生源地信用助学贷款",
            "steps": "第八章 贷款申请流程与时间要求",
        },
        articles={"steps": "第十六条"},
        keywords=("助学贷款", "生源地", "16000", "贴息"),
        recommended=True,
    ),
    _item(
        "work-study", "scholarship", "勤工助学申请",
        "国家助学贷款与勤工助学管理办法",
        "学校设置校内勤工助学岗位，面向家庭经济困难学生；上岗前须完成岗前培训，考核合格后安排岗位。",
        sections={
            "conditions": "第三章 勤工助学",
            "steps": "第三章 勤工助学",
            "faq": "第七章 常见问题",
        },
        keywords=("勤工助学", "校内岗位", "岗前培训"),
    ),
    _item(
        "hardship-certification", "scholarship", "家庭经济困难学生认定",
        "国家助学贷款与勤工助学管理办法",
        "认定结果分为特别困难、困难、一般困难三档，每学年 9 月集中认定一次；"
        "认定结果是申请励志奖学金、助学贷款与勤工助学的前置条件。",
        sections={
            "conditions": "第五章 家庭经济困难学生认定",
            "steps": "第五章 家庭经济困难学生认定",
        },
        keywords=("困难认定", "家庭经济困难", "三档"),
    ),
    # ---------------------------------------------------------------- 评奖评优
    _item(
        "competition-award", "honors", "学科竞赛获奖认定与奖励",
        "学科竞赛与创新创业项目管理规定",
        "竞赛按主办层级分为 A、B、C 三类，获奖可申请创新创业学分与奖金；"
        "级别认定每年 3 月、9 月各受理一次。",
        sections={
            "conditions": "第一章 竞赛分级与认定",
            "steps": "第四章 申报与认定流程",
            "faq": "第六章 常见问题",
        },
        keywords=("学科竞赛", "A 类", "创新创业学分", "奖金"),
        recommended=True,
    ),
    _item(
        "innovation-project", "honors", "创新创业训练项目申报",
        "学科竞赛与创新创业项目管理规定",
        "大学生创新创业训练计划分国家级、省级、校级三级，按级别给予不同资助经费，"
        "申报与认定按第四章流程办理。",
        sections={
            "conditions": "第三章 创新创业训练项目",
            "steps": "第四章 申报与认定流程",
        },
        keywords=("大创", "创新创业训练", "项目申报"),
    ),
    # ------------------------------------------------------------ 考试与等级考试
    _item(
        "cet", "exams", "全国大学英语四六级考试报名",
        "全国大学英语四六级考试报名须知",
        "四级面向全日制在校本科生、专科生和研究生（一年级第二学期起可报考）；"
        "报考六级须四级成绩达到 425 分以上。",
        sections={
            "conditions": "第一章 报名资格",
            "steps": "第二章 报名与缴费",
            "faq": "第七章 常见问题",
        },
        keywords=("四六级", "CET", "425", "准考证"),
        recommended=True,
    ),
    _item(
        "ncre", "exams", "全国计算机等级考试报名",
        "计算机等级考试与职业资格证书管理办法",
        "全国计算机等级考试（NCRE）每年 3 月和 9 月各举行一次，本校设二级和三级考点，"
        "通过教育部考试中心报名系统在线报名缴费。",
        sections={
            "conditions": "第一章 全国计算机等级考试",
            "steps": "第六章 考试组织",
            "faq": "第七章 常见问题",
        },
        keywords=("NCRE", "计算机等级", "二级", "报名费"),
    ),
    _item(
        "putonghua", "exams", "普通话水平测试报名",
        "计算机等级考试与职业资格证书管理办法",
        "普通话水平测试每年 4 月和 10 月各举行一次，报名费 50 元，"
        "具体考场与时间安排见第六章。",
        sections={
            "conditions": "第二章 普通话水平测试",
            "steps": "第六章 考试组织",
        },
        keywords=("普通话", "水平测试", "二甲"),
    ),
    _item(
        "vocational-cert", "exams", "职业资格证书与学分认定",
        "计算机等级考试与职业资格证书管理办法",
        "在校期间取得与所学专业相关的职业资格证书，可以申请认定创新创业学分，"
        "按第五章流程在线提交证书扫描件。",
        sections={
            "conditions": "第三章 职业资格证书",
            "steps": "第五章 证书认定流程",
        },
        keywords=("职业资格", "证书", "学分认定"),
    ),
    # ---------------------------------------------------------------- 学籍与成绩
    _item(
        "suspension-resume", "academic", "休学与复学办理",
        "学籍管理规定",
        "休学、复学等学籍异动需提交学籍异动申请表、辅导员意见与佐证材料，"
        "材料齐全的 5 个工作日内办结。",
        sections={
            "conditions": "第三章 休学与复学",
            "steps": "第十章 学籍事务办理流程与时限",
        },
        articles={"steps": "第二十七条"},
        keywords=("休学", "复学", "学籍异动"),
    ),
    _item(
        "major-transfer", "academic", "转专业申请",
        "学籍管理规定",
        "转专业由本人申请、转出学院审核、转入学院考核、教务处复核公示；"
        "申请截止时间为每年 5 月 20 日 17:00。",
        sections={
            "conditions": "第四章 转专业",
            "steps": "第十章 学籍事务办理流程与时限",
        },
        articles={"steps": "第二十四条"},
        keywords=("转专业", "5 月 20 日", "考核"),
        recommended=True,
    ),
    _item(
        "grade-review", "academic", "成绩复核申请",
        "课程考核与成绩管理办法",
        "对成绩有异议可在规定期限内申请复核，复核范围与办理方式见第四章；"
        "成绩查询与使用规则见第七章。",
        sections={
            "conditions": "第四章 成绩复核与查询",
            "steps": "第四章 成绩复核与查询",
            "faq": "第七章 成绩使用与常见问题",
        },
        keywords=("成绩复核", "成绩查询", "异议"),
    ),
    _item(
        "makeup-exam", "academic", "缓考、补考与重修",
        "课程考核与成绩管理办法",
        "缓考须事先申请并附证明，补考与重修按第三章规定的范围与时间办理。",
        sections={
            "conditions": "第三章 缓考、补考与重修",
            "steps": "第三章 缓考、补考与重修",
        },
        keywords=("缓考", "补考", "重修"),
    ),
    _item(
        "credit-exemption", "academic", "免修、免听与学分认定",
        "课程考核与成绩管理办法",
        "符合条件的课程可申请免修、免听或学分认定，申请条件与认定方式见第五章。",
        sections={
            "conditions": "第五章 免修、免听与学分认定",
            "steps": "第五章 免修、免听与学分认定",
        },
        keywords=("免修", "免听", "学分认定"),
    ),
    # ------------------------------------------------------------ 宿舍与校园生活
    _item(
        "dorm-change", "campus-life", "调宿申请",
        "学生宿舍管理规定",
        "每学期第 2 周至第 14 周受理调宿，材料齐全的 3 个工作日内办结；"
        "学期末两周与新生入学周不受理。",
        sections={
            "conditions": "第三章 卫生与调宿",
            "materials": "第八章 调宿、退宿与住宿事务办理",
            "steps": "第八章 调宿、退宿与住宿事务办理",
        },
        articles={"materials": "第二十条", "steps": "第十九条"},
        keywords=("调宿", "换宿", "床位"),
        recommended=True,
    ),
    _item(
        "dorm-checkout", "campus-life", "退宿与离校",
        "学生宿舍管理规定",
        "毕业生退宿截止 6 月 30 日；中途退宿须提前 7 天提交申请，"
        "清点物品、交回钥匙并结清水电费后办理。",
        sections={
            "conditions": "第四章 退宿与离校",
            "steps": "第八章 调宿、退宿与住宿事务办理",
        },
        articles={"steps": "第二十二条"},
        keywords=("退宿", "离校", "6 月 30 日"),
    ),
    _item(
        "card-replacement", "campus-life", "校园卡补办",
        "校园卡与网络服务使用指南",
        "校园卡遗失应立即挂失；补办需本人持身份证件到学生服务大厅办理，工本费 10 元。",
        sections={
            "conditions": "第二章 补办与注销",
            "steps": "第二章 补办与注销",
            "faq": "第七章 常见问题",
        },
        keywords=("校园卡", "补办", "挂失", "工本费"),
        recommended=True,
    ),
    _item(
        "campus-network", "campus-life", "校园网开通与故障报修",
        "校园卡与网络服务使用指南",
        "校园网覆盖教学楼、图书馆与宿舍区，学生账号按月计费；"
        "网络故障可通过「校园服务」小程序报修。",
        sections={
            "conditions": "第三章 校园网",
            "steps": "第四章 故障报修",
            "faq": "第七章 常见问题",
        },
        keywords=("校园网", "网络报修", "账号"),
    ),
    _item(
        "clinic-visit", "campus-life", "校医院就诊与医保报销",
        "校医院就诊与心理健康服务指南",
        "校医院门诊每日 8:00–20:00、急诊 24 小时值班；"
        "校内就诊医保范围内费用报销比例为 80%，转诊到定点医院按规定执行。",
        sections={
            "conditions": "第一章 门诊与急诊",
            "steps": "第二章 医保与报销",
            "faq": "第七章 常见问题",
        },
        keywords=("校医院", "医保", "报销", "转诊"),
    ),
    _item(
        "psych-service", "campus-life", "心理健康服务预约",
        "校医院就诊与心理健康服务指南",
        "学校心理健康教育中心提供免费个体心理咨询，每人每学期可预约 8 次。",
        sections={
            "conditions": "第三章 心理健康服务",
            "steps": "第三章 心理健康服务",
        },
        keywords=("心理咨询", "心理健康", "免费预约"),
    ),
    # ---------------------------------------------------------------- 图书馆服务
    _item(
        "book-borrow", "library", "图书借阅与续借",
        "图书馆借阅与自习空间管理办法",
        "本科生可同时借阅图书 10 册、借期 30 天，可续借 2 次；"
        "凭校园卡在自助借还机办理，无需另行提交材料。",
        sections={
            "conditions": "第二章 借阅规则",
            "steps": "第八章 办理流程与时限",
        },
        articles={"steps": "第二十条"},
        keywords=("借阅", "续借", "10 册", "30 天"),
        recommended=True,
    ),
    _item(
        "study-room", "library", "自习与研讨空间预约",
        "图书馆借阅与自习空间管理办法",
        "自习座位通过图书馆小程序预约，每次最长 4 小时；"
        "研讨间须提前 1 天预约且使用人数 3 人以上。",
        sections={
            "conditions": "第三章 自习与研讨空间",
            "steps": "第八章 办理流程与时限",
        },
        articles={"steps": "第二十三条"},
        keywords=("自习", "座位预约", "研讨间"),
    ),
    _item(
        "remote-db", "library", "电子资源与校外访问",
        "图书馆借阅与自习空间管理办法",
        "图书馆订购中外文数据库 40 余个，校外访问与文献传递规则见第五章。",
        sections={
            "conditions": "第五章 电子资源与校外访问",
            "steps": "第五章 电子资源与校外访问",
        },
        keywords=("数据库", "校外访问", "文献传递"),
    ),
    # ---------------------------------------------------------------- 就业与实习
    _item(
        "postgraduate-recommend", "career", "推荐免试攻读研究生申请",
        "推荐免试攻读研究生遴选办法",
        "推免实行学生申请、学院遴选、学校审定、结果公示；"
        "名额按应届本科毕业生人数与学科建设情况分配。",
        sections={
            "conditions": "第二章 申请条件",
            "materials": "第五章 材料清单与常见问题",
            "steps": "第三章 工作程序与时间安排",
            # 材料与常见问题同章：只取"申请材料"那一条，常见问题明确不取
            # （否则会把材料条目当成"问题"再列一遍）
            "faq": "",
        },
        articles={"materials": "第十一条"},
        keywords=("推免", "保研", "遴选", "综合成绩"),
    ),
    _item(
        "postgraduate-exam-support", "career", "考研支持与考试补助",
        "学生考研与出国（境）交流支持办法",
        "学校设考研专用自习区并配备学业导师；家庭经济困难学生参加研究生招生考试"
        "可申请每人 300 元交通补助，每年 11 月集中受理。",
        sections={
            "conditions": "第一章 考研支持",
            "steps": "第五章 办理流程与时间节点",
            "faq": "第七章 常见问题",
        },
        keywords=("考研", "自习区", "交通补助 300 元"),
    ),
    _item(
        "overseas-exchange", "career", "出国（境）交流申请",
        "学生考研与出国（境）交流支持办法",
        "校际交换生项目每年 3 月和 9 月各受理一次，申请人应为二年级以上学生；"
        "英语授课项目须提供雅思 6.0 或托福 80 分以上成绩。",
        sections={
            "conditions": "第二章 出国（境）交流",
            "steps": "第五章 办理流程与时间节点",
        },
        keywords=("交换生", "出国", "雅思", "托福"),
    ),
    # ------------------------------------------------------------ 请假与证明办理
    _item(
        "leave-request", "service", "请假与销假",
        "学生事务办理指南",
        "3 天以内由辅导员审批，3 天以上须附校医院证明，2 周以上办理休学或缓考；"
        "请假与销假统一在线上服务大厅提交。",
        sections={
            # 本指南的「材料要求」是四类事务通用的，不是请假专属；列在请假卡片里
            # 会被误读成"请假必须带成绩单"，所以明确关掉
            "materials": "",
            "steps": "五、高频事项办理指引",
            "faq": "七、常见问题",
        },
        lines={"steps": "请假"},
        keywords=("请假", "销假", "辅导员", "校医院证明"),
        recommended=True,
    ),
    _item(
        "enrollment-certificate", "service", "在读证明办理",
        "学生事务办理指南",
        "在读证明可在服务大厅自助机用本人校园卡打印，办理时限 2 个工作日，免费办理。",
        sections={
            "materials": "三、材料要求",
            "steps": "五、高频事项办理指引",
            "faq": "七、常见问题",
        },
        lines={"steps": "在读证明"},
        keywords=("在读证明", "自助机", "免费"),
        recommended=True,
    ),
    _item(
        "transcript", "service", "成绩单打印",
        "学生事务办理指南",
        "成绩单须为教务系统导出并带验证码，可在自助机即时打印，每学期免费 3 份。",
        sections={
            "materials": "三、材料要求",
            "steps": "五、高频事项办理指引",
        },
        lines={"steps": "成绩单"},
        keywords=("成绩单", "打印", "验证码"),
        recommended=True,
    ),
    _item(
        "status-change-service", "service", "学籍异动申请（服务大厅）",
        "学生事务办理指南",
        "休学、复学、转专业等学籍异动在线上服务大厅提交申请并附证明材料，"
        "5 个工作日办结。",
        sections={
            "materials": "三、材料要求",
            "steps": "二、办理流程",
            "faq": "七、常见问题",
        },
        keywords=("学籍异动", "服务大厅", "5 个工作日"),
    ),
]

_ITEM_BY_KEY = {item["key"]: item for item in GUIDE_ITEMS}

# 字段 → 章节关键词。顺序即优先级：先找"申请条件"，再退到"评审标准"，最后才用泛化的"条件"。
_FIELD_KEYWORDS: dict[str, tuple[str, ...]] = {
    "conditions": ("申请条件", "申报条件", "报名资格", "评审标准", "资格", "条件"),
    "materials": ("材料", "清单"),
    "steps": ("办理流程", "流程", "程序", "办理", "申报", "时限"),
    "faq": ("常见问题", "问答"),
}

_FIELD_LABEL = {
    "conditions": "申请条件",
    "materials": "材料清单",
    "steps": "办理步骤",
    "faq": "常见问题",
}

# ---------------------------------------------------------------- 文本处理
_PAGE_RE = re.compile(r"^第\s*\d+\s*页\s*/\s*共\s*\d+\s*页?$")
_ARTICLE_RE = re.compile(r"^\s*(第[一二三四五六七八九十百零]+条)")
_BULLET_RE = re.compile(
    r"^\s*(第[一二三四五六七八九十百零]+[条章]|[（(][一二三四五六七八九十\d]+[）)]"
    r"|\d+[.、)]|[一二三四五六七八九十]+、|[-•·])"
)
_END_RE = re.compile(r"[。；：！？]\s*$")


def _unwrap(lines: list[str]) -> list[str]:
    """把 PDF 硬换行接回句子。

    chunk 里的换行有两类，长得一模一样：**排版换行**（一句话被版心切断）和
    **清单换行**（每条一个短行，末尾没有标点）。只按"上一行有没有句号"判断，
    会把三条材料并成一句（实测：「身份证件仅需核验，不留存复印件 / 成绩单须为
    教务系统导出并带验证码 / 委托他人办理须提供授权委托书」）。

    这里改用**版心宽度**判断：同一份 PDF 的正文行宽是固定的（实测 38 个字符），
    只有排满一行的才可能被切断；短行是清单项，不合并。宽度取"不超过 45 字符的
    最长行"——直接把 70 字的行算进去会把阈值抬到 66，反而一条都合不上（示例数据里
    有些行本身就是两行拼在一起的）。
    """
    if not lines:
        return []
    lengths = [len(line) for line in lines]
    narrow = [length for length in lengths if length <= 45]
    width = max(narrow) if narrow else max(lengths)
    threshold = max(width - 2, 30)
    out: list[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if (out and not _END_RE.search(out[-1]) and not _BULLET_RE.match(line)
                and len(out[-1]) >= threshold):
            out[-1] = out[-1] + line
        else:
            out.append(line)
    return out


def _clean(content: str, section: str | None, doc_title: str) -> str:
    lines = [line.strip() for line in content.splitlines()]
    lines = [line for line in lines if line and not _PAGE_RE.match(line)]
    # 首行常是文件标题（示例数据里带「示例大学」前缀，不能只做等值比较）
    if lines and (lines[0] == doc_title or lines[0].endswith(doc_title)):
        lines.pop(0)
    if lines and section and lines[0] == section:
        lines.pop(0)
    return "\n".join(_unwrap(lines))


def _slice_article(text: str, article: str) -> str | None:
    """截取某一条：从该条首行到下一条之前。找不到返回 None（调用方退回整章）。"""
    lines = text.split("\n")
    start = None
    for index, line in enumerate(lines):
        matched = _ARTICLE_RE.match(line)
        if matched and matched.group(1) == article:
            start = index
            break
    if start is None:
        return None
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if _ARTICLE_RE.match(lines[index]):
            end = index
            break
    return "\n".join(lines[start:end]).strip()


def _base_title(title: str) -> str:
    """去掉尾部版本括注：「本科生奖学金评审办法（2026年版）」→「本科生奖学金评审办法」。"""
    return re.sub(r"[（(][^）)]*[）)]\s*$", "", title or "").strip()


# ---------------------------------------------------------------- 解析
def _load_context():
    docs = db.session.execute(
        db.select(Document).where(Document.deleted_at.is_(None))
        .order_by(Document.created_at)
    ).scalars().all()
    chunks = db.session.execute(
        db.select(DocumentChunk).order_by(DocumentChunk.document_id, DocumentChunk.chunk_index)
    ).scalars().all()
    grouped: dict[str, list[DocumentChunk]] = {}
    for chunk in chunks:
        grouped.setdefault(chunk.document_id, []).append(chunk)

    rules = db.session.execute(
        db.select(PolicyRule).where(PolicyRule.review_status == REVIEW_APPROVED)
    ).scalars().all()
    rules_by_doc: dict[str, list[PolicyRule]] = {}
    for rule in rules:
        rules_by_doc.setdefault(rule.document_id, []).append(rule)

    return docs, grouped, rules_by_doc


def _pick_document(docs: list[Document], doc_key: str) -> Document | None:
    """按标题键选**当前有效**的那一版。

    这是"政策更新后指南自动跟随"的全部实现：解析时把 active 排在最前，
    没有 active 才退到"未被替代的"，再退到最新的历史版本。
    文件被删（deleted_at 非空）不会进候选，事项随之变成 missing_policy。
    """
    candidates = [doc for doc in docs if _base_title(doc.title) == doc_key]
    if not candidates:
        return None

    def rank(doc: Document):
        return (
            1 if doc.effective_status == EFFECTIVE_ACTIVE else 0,
            0 if doc.effective_status == EFFECTIVE_SUPERSEDED else 1,
            doc.academic_year or "",
            doc.created_at.isoformat() if doc.created_at else "",
        )

    return sorted(candidates, key=rank)[-1]


def _find_chunk(chunks: list[DocumentChunk], section: str | None):
    if not section:
        return None
    for chunk in chunks:
        if chunk.section_title == section:
            return chunk
    for chunk in chunks:
        title = chunk.section_title or ""
        if section in title or title in section:
            return chunk
    return None


def _auto_chunk(chunks: list[DocumentChunk], field: str):
    for keyword in _FIELD_KEYWORDS[field]:
        for chunk in chunks:
            if keyword in (chunk.section_title or ""):
                return chunk
    return None


def _audience(doc: Document, chunks: list[DocumentChunk], rules: list[PolicyRule]):
    """适用对象：优先取原文里"适用/面向"的那句话，其次取规则里的奖项标签。

    两者都没有就返回 None，前端显示"原文未单列适用对象"。**不从文件标题猜**——
    「学生宿舍管理规定」到底管不管研究生，猜错了比留空更糟。
    """
    for chunk in chunks[:2]:
        text = _clean(chunk.content, chunk.section_title, doc.title)
        for sentence in re.split(r"[。；\n]", text):
            sentence = sentence.strip()
            if 6 <= len(sentence) <= 90 and ("适用" in sentence or "面向" in sentence):
                # 「第二条 本办法适用于…」→ 去掉条文号，卡片上只留内容
                sentence = re.sub(r"^第[一二三四五六七八九十百零]+条\s*", "", sentence)
                return {
                    "text": sentence,
                    "source": "policy",
                    "section": chunk.section_title,
                }
    groups = sorted({rule.target_group for rule in rules if rule.target_group})
    if groups:
        return {"text": "、".join(groups), "source": "rules", "section": None}
    return None


def _drop_intro(text: str) -> str:
    """去掉章节导语。

    「三、材料要求」第一行是"材料不齐是最常见的退回原因，提交前请逐项核对。"
    ——它解释的是这一章为什么重要，不是材料清单里的一条。判据是：非条文/编号开头、
    以句号结尾、且后面还有至少两行。只删首行，正文一条不动。
    """
    lines = [line for line in text.split("\n") if line.strip()]
    if (len(lines) >= 3 and not _BULLET_RE.match(lines[0])
            and lines[0].endswith("。") and not _ARTICLE_RE.match(lines[0])):
        return "\n".join(lines[1:])
    return text


def _faq_list(text: str) -> list[str]:
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    # 常见问题章的首行常是导语（「这几类问题占了咨询量的一半…」），不是问题本身。
    # 判据：不是"第X条"开头、也不以分号结尾。只丢首行，不动后面的真条目。
    if len(lines) >= 3 and not _ARTICLE_RE.match(lines[0]) and not lines[0].endswith("；"):
        lines = lines[1:]
    if len(lines) <= 1:
        parts = [part.strip(" 。；") for part in re.split(r"[；;]", text)]
        return [part for part in parts if len(part) >= 8][:12]
    return lines[:12]


def _doc_meta(doc: Document | None, docs: list[Document]) -> dict | None:
    if doc is None:
        return None
    superseded_by = None
    if doc.effective_status == EFFECTIVE_SUPERSEDED:
        for other in docs:
            if (other.id != doc.id
                    and _base_title(other.title) == _base_title(doc.title)
                    and other.effective_status == EFFECTIVE_ACTIVE):
                superseded_by = {"id": other.id, "title": other.title}
                break
    return {
        "id": doc.id,
        "title": doc.title,
        "effective_status": doc.effective_status,
        "academic_year": doc.academic_year,
        "version_label": doc.version_label,
        "department": doc.department,
        "published_at": doc.published_at.isoformat() if doc.published_at else None,
        "parse_status": doc.parse_status,
        "chunk_count": doc.chunk_count,
        "superseded_by": superseded_by,
    }


def _resolve(spec: dict, docs, chunks_by_doc, rules_by_doc) -> dict:
    doc = _pick_document(docs, spec["doc_key"])
    category = _CATEGORY_BY_ID[spec["category"]]

    if doc is None:
        return {
            "key": spec["key"],
            "category": spec["category"],
            "category_label": category["label"],
            "title": spec["title"],
            "summary": spec["summary"],
            "keywords": list(spec["keywords"]),
            "recommended": spec["recommended"],
            "availability": "missing_policy",
            "availability_label": "暂未收录相关政策",
            "audience": None,
            "document": None,
            "blocks": [],
            "faq": [],
            "duplicate_fields": [],
            "actions": {
                "ask": {"question": spec["question"], "document_id": None},
                "match": {"available": False, "document_id": None,
                          "reason": "该事项还没有收录政策文件，无法进行资格匹配"},
                "plan": {"available": False, "document_id": None,
                         "reason": "该事项还没有收录政策文件，无法生成办理计划"},
            },
            "coverage": {"sections_found": 0, "sections_expected": 3},
        }

    chunks = chunks_by_doc.get(doc.id, [])
    rules = rules_by_doc.get(doc.id, [])
    usable = doc.parse_status not in ("failed", "empty") and bool(chunks)

    blocks: list[dict] = []
    faq: list[str] = []
    if usable:
        for field in ("conditions", "materials", "steps", "faq"):
            section = spec["sections"].get(field)
            # 空字符串 = 该事项**明确不取**这一节（如「材料清单与常见问题」合并成一章时，
            # 不能再把材料当常见问题抓一遍）。None 才是"没指定，走自动匹配"。
            if section == "":
                continue
            chunk = _find_chunk(chunks, section) if section else _auto_chunk(chunks, field)
            if chunk is None:
                continue
            text = _clean(chunk.content, chunk.section_title, doc.title)
            article = spec["articles"].get(field)
            if article:
                sliced = _slice_article(text, article)
                # 条文没找到就用整章：宁可多给，不可少给；`article` 置空让前端知道
                # 这是整章而不是那一条
                if sliced:
                    text = sliced
                else:
                    article = None
            if not text:
                continue
            needle = spec["lines"].get(field)
            if needle:
                kept = [line for line in text.split("\n") if needle in line]
                # 一行都没命中说明关键词写错了，宁可退回整章也不要给出空内容
                if kept:
                    text = "\n".join(kept)
            if field == "faq":
                faq = _faq_list(text)
                continue
            if field in ("materials", "steps"):
                text = _drop_intro(text)
            blocks.append({
                "field": field,
                "label": _FIELD_LABEL[field],
                "section": chunk.section_title,
                "page": chunk.page_number,
                "article": article,
                "text": text,
            })

    # 条件与步骤常写在同一章（缓考、勤工助学等）。同一段原文展示两遍只会让人
    # 以为读了两节，实际一个字都没多——保留第一次出现，后面记进 `duplicate_fields`。
    deduped: list[dict] = []
    seen_sections: set[tuple] = set()
    duplicate_fields: list[str] = []
    for block in blocks:
        signature = (block["section"], block["article"])
        if signature in seen_sections:
            duplicate_fields.append(block["field"])
            continue
        seen_sections.add(signature)
        deduped.append(block)
    blocks = deduped

    condition_rules = [r for r in rules if r.rule_type in CONDITION_RULE_TYPES]
    plan_rules = [r for r in rules if r.rule_type in (RULE_MATERIAL, RULE_DEADLINE, RULE_PROCESS)]

    return {
        "key": spec["key"],
        "category": spec["category"],
        "category_label": category["label"],
        "title": spec["title"],
        "summary": spec["summary"],
        "keywords": list(spec["keywords"]),
        "recommended": spec["recommended"],
        "availability": "ok" if usable else "no_text",
        "availability_label": "已收录" if usable else "政策文件暂无可解析正文",
        "audience": _audience(doc, chunks, rules) if usable else None,
        "document": _doc_meta(doc, docs),
        "blocks": blocks,
        "faq": faq,
        "duplicate_fields": duplicate_fields,
        "actions": {
            "ask": {
                "question": spec["question"],
                "document_id": doc.id,
            },
            "match": {
                "available": bool(condition_rules),
                "document_id": doc.id,
                "approved_condition_rules": len(condition_rules),
                "reason": None if condition_rules else "该文件还没有已复核的条件类规则；可先到规则管理抽取并复核",
            },
            "plan": {
                "available": bool(plan_rules),
                "document_id": doc.id,
                "approved_rules": len(rules),
                "reason": None if plan_rules else "该文件的规则里没有可执行的材料、时间或流程条款",
            },
        },
        "coverage": {
            "sections_found": len(blocks) + (1 if faq else 0),
            "sections_expected": 4,
        },
    }


def _card(item: dict) -> dict:
    """列表卡片：只保留卡片要用的字段，正文留在详情里。

    卡片上要**同时**给出条件、材料、流程三行的开头（需求里事项卡片就是这么列的），
    但每行只截 90 字：卡片是"要不要点进去"的判断依据，不是阅读区。
    三行都给，比只给条件更接近一个学生真正关心的三个问题：
    我够不够格 / 带什么 / 跑几趟。
    """
    def preview(field: str, limit: int = 90) -> str:
        for block in item["blocks"]:
            if block["field"] == field:
                text = re.sub(r"\s+", " ", block["text"]).strip()
                return (text[:limit] + "…") if len(text) > limit else text
        return ""

    conditions = preview("conditions")
    if not conditions:
        conditions = preview("steps")
    materials = preview("materials", 72)
    steps = preview("steps", 72)

    return {
        "key": item["key"],
        "category": item["category"],
        "category_label": item["category_label"],
        "title": item["title"],
        "summary": item["summary"],
        "keywords": item["keywords"],
        "recommended": item["recommended"],
        "availability": item["availability"],
        "availability_label": item["availability_label"],
        "audience": item["audience"],
        "document": item["document"],
        "conditions_preview": conditions,
        "materials_preview": materials,
        "steps_preview": steps,
        "match_available": item["actions"]["match"]["available"],
        "plan_available": item["actions"]["plan"]["available"],
    }


def _resolve_all() -> list[dict]:
    docs, chunks_by_doc, rules_by_doc = _load_context()
    return [_resolve(spec, docs, chunks_by_doc, rules_by_doc) for spec in GUIDE_ITEMS]


# ---------------------------------------------------------------- 路由
@bp.get("/guides")
@login_required
def overview():
    """指南首页：分类 + 常用事项。

    分类里的 `item_count` 是**编目条数**，`available_count` 是有政策依据的条数。
    两者不等时前端要显示"已收录 N / M"，让空状态有解释——只说"暂无"而不说
    "本该有几条"，读者分不清是知识库没资料还是系统坏了。
    """
    items = _resolve_all()
    cards = [_card(item) for item in items]

    categories = []
    for meta in CATEGORIES:
        own = [item for item in items if item["category"] == meta["id"]]
        available = [item for item in own if item["availability"] == "ok"]
        categories.append({
            **meta,
            "item_count": len(own),
            "available_count": len(available),
            "empty": not available,
            "document_count": len({item["document"]["id"] for item in available if item["document"]}),
        })

    recommended = [
        card for card in cards
        if card["recommended"] and card["availability"] == "ok"
    ][:8]

    available_items = [item for item in items if item["availability"] == "ok"]
    return ok({
        "categories": categories,
        "recommended": recommended,
        "stats": {
            "category_count": len(CATEGORIES),
            "item_count": len(items),
            "available_count": len(available_items),
            "missing_count": len(items) - len(available_items),
            "empty_category_count": sum(1 for c in categories if c["empty"]),
            "document_count": len({
                item["document"]["id"] for item in available_items if item["document"]
            }),
        },
        "notice": "指南正文为政策原文摘录，按「现行有效」版本整理；政策换版后自动指向新版本。",
    })


@bp.get("/guides/items")
@login_required
def list_items():
    category = (request.args.get("category") or "").strip()
    keyword = (request.args.get("q") or "").strip().lower()
    if category and category not in _CATEGORY_BY_ID:
        raise InvalidParam(f"未知的分类：{category}")
    page, page_size = page_args()

    items = _resolve_all()
    cards = [_card(item) for item in items]

    if category:
        cards = [card for card in cards if card["category"] == category]

    if keyword:
        def hit(card: dict) -> bool:
            haystack = " ".join([
                card["title"],
                card["summary"],
                card["category_label"],
                " ".join(card["keywords"]),
                (card["document"] or {}).get("title") or "",
            ]).lower()
            return keyword in haystack

        cards = [card for card in cards if hit(card)]

    total = len(cards)
    start = (page - 1) * page_size
    window = cards[start:start + page_size]
    return ok({
        **paginated(window, total, page, page_size),
        "category": _CATEGORY_BY_ID.get(category) if category else None,
        "query": keyword,
        "coverage": {
            "total": total,
            "available": sum(1 for card in cards if card["availability"] == "ok"),
        },
    })


@bp.get("/guides/items/<key>")
@login_required
def item_detail(key: str):
    spec = _ITEM_BY_KEY.get(key)
    if spec is None:
        raise GuideNotFound()

    items = _resolve_all()
    by_key = {item["key"]: item for item in items}
    detail = by_key[key]
    related = [
        _card(item) for item in items
        if item["category"] == detail["category"] and item["key"] != key
    ][:3]

    return ok({
        **detail,
        "related": related,
        "source_note": (
            "「申请条件 / 材料清单 / 办理步骤 / 常见问题」均为政策原文摘录，"
            "未做改写；「事项简介」是编辑摘要，用于快速判断是否符合，具体以原文为准。"
        ),
    })
