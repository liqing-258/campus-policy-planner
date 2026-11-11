"""意图识别节点（设计 §7.6 的第 ① 个节点）。

**它不调模型**。设计里这一格写的是"是（可降级为规则匹配）"，M6 直接取降级形态：
用关键词表判定事项类型。理由有三个，都不是省事：

1. 意图识别的输出只有"这问题属于哪一类事项"这一个标签，候选空间是**封闭的十几类**。
   让模型来选，代价是每次提问多 1–3 秒、多一次可能的超时与一种不确定性，
   换来的准确率提升在这个封闭集合上很有限。
2. 一旦调模型，`/qa/ask` 的延迟里就多一段不可解释的时间——而节点轨迹要展示耗时，
   "意图识别 2.3 秒"会被追问在做什么，答案只能是"调了一次模型猜分类"。
3. 判错代价不对称：分类只用于**给出下一步建议**（"要不要试试资格匹配"），
   不影响答案本身。为了一个建议去付模型调用，不划算。

**它的输出被谁用**：不是装饰。`needs_profile` 决定响应里是否追加
`next_actions` 建议（去资格匹配 / 去生成计划），前端据此渲染可点按钮。
如果这里判成 `other`，就不建议——不硬凑。
"""

from __future__ import annotations

# 事项类型 → (中文标签, 关键词)。关键词取政策原文里真会出现的词，
# 不取同义词扩展（"奖学金"不扩成"奖助学金""奖优"）——扩展越多，误判越多。
MATTER_TYPES: dict[str, tuple[str, tuple[str, ...]]] = {
    "scholarship": ("奖助学金", ("奖学金", "励志", "助学金", "资助", "补助", "减免")),
    "loan": ("助学贷款", ("助学贷款", "生源地", "贷款", "还款", "贴息")),
    "major_change": ("转专业", ("转专业", "转系", "专业调整", "跨学院")),
    "credit": ("学分与成绩", ("学分", "成绩", "绩点", "补考", "重修", "认定", "免修")),
    "leave": ("请假与学籍", ("请假", "销假", "旷课", "休学", "复学", "退学", "考勤")),
    "dorm": ("住宿管理", ("宿舍", "寝室", "住宿", "退宿", "调宿")),
    "graduation": ("毕业与学位", ("毕业", "学位", "论文", "答辩", "离校")),
    "honor": ("评优评先", ("评优", "三好", "优秀学生", "先进个人", "标兵")),
}

# 出现这些词说明用户在问"我够不够"，才值得建议去资格匹配。
# 只问流程（"什么时候截止"）不该被推去做画像填写。
ELIGIBILITY_HINTS = (
    "条件", "资格", "要求", "门槛", "能不能", "可不可以", "够不够",
    "是否符合", "有没有资格", "申请条件", "需要满足",
)


def classify(question: str) -> dict:
    """返回 `{matter_type, label, needs_profile, matched_keywords}`。

    `matter_type` 取第一个命中的类别（按 MATTER_TYPES 声明顺序），
    同时把**所有**命中的关键词返回——两个类别都命中是常态
    （"奖学金"+"成绩"），把它藏起来会让后续排查看不懂为什么分到了 A 而不是 B。
    """
    text = question or ""
    hits: dict[str, list[str]] = {}
    for key, (_, keywords) in MATTER_TYPES.items():
        matched = [word for word in keywords if word in text]
        if matched:
            hits[key] = matched

    matter_type = next(iter(hits), "other")
    label = MATTER_TYPES[matter_type][0] if matter_type in MATTER_TYPES else "其他事项"
    needs_profile = any(hint in text for hint in ELIGIBILITY_HINTS) and matter_type != "other"
    return {
        "matter_type": matter_type,
        "label": label,
        # 命中的类别可能有多个，按声明顺序给出来
        "all_matters": list(hits),
        "matched_keywords": [word for words in hits.values() for word in words],
        "needs_profile": needs_profile,
    }


def next_actions(intent: dict) -> list[dict]:
    """根据意图给出可点的下一步。**只在有依据时给**，不硬凑推荐位。"""
    if not intent.get("needs_profile"):
        return []
    return [
        {
            "action": "match",
            "label": f"去「资格匹配」核对我是否符合{intent['label']}条件",
            "reason": "这个问题在问申请条件，填一份画像可以直接得到三态结论与依据",
        },
        {
            "action": "plan",
            "label": "生成办事计划",
            "reason": "匹配确认后可以一键生成带截止日期的任务清单",
        },
    ]
