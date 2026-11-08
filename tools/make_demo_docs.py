"""生成虚构演示政策文件（红线 X-03：不得使用真实学校/真实学生数据）。

产物（`demo/`，随仓库入库）：
    01-本科生奖学金评审办法（演示样例）.pdf    正常 PDF，4 页，含章节与页码
    02-学生事务办理指南（演示样例）.docx        正常 DOCX，含标题层级与表格
    03-扫描件样例（演示样例）.pdf               无文本层（图片），用于 T-04
    04-空文本样例（演示样例）.txt               仅空白，用于 T-05
    05-改名的假文件（演示样例）.pdf              实际是 MZ 头，用于 T-01

用法：
    .venv\\Scripts\\python.exe tools\\make_demo_docs.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEMO_DIR = ROOT / "demo"
FONT = "china-s"  # PyMuPDF 内置简体中文字体，无需外挂字体文件

# ---------- 内容：全部虚构，机构名统一用「示例大学」 ----------
PAGES: list[list[tuple[str, bool]]] = [
    [
        ("示例大学本科生奖学金评审办法（演示样例）", True),
        ("第一章 总则", True),
        ("第一条 为规范本校本科生奖学金评审工作，激励学生勤奋学习、全面发展，"
         "根据国家有关规定并结合本校实际，制定本办法。", False),
        ("第二条 本办法适用于具有本校正式学籍的全日制在校本科学生。"
         "交换生、旁听生、非学历教育学生不适用本办法。", False),
        ("第三条 奖学金评审坚持公开、公平、公正的原则，实行学生申请、"
         "学院初审、学校评审、结果公示的四级流程。", False),
    ],
    [
        ("第二章 申请条件", True),
        ("第四条 申请国家励志奖学金，应当同时具备下列条件：", False),
        ("（一）具有中华人民共和国国籍，且为全日制在校本科二年级以上（含二年级）学生；", False),
        ("（二）同一学年内未获得国家奖学金；", False),
        ("（三）学年平均学分绩点不低于 3.0，且专业排名位于前 30%；", False),
        ("（四）经学校家庭经济困难学生认定程序认定为家庭经济困难；", False),
        ("（五）学年内无纪律处分记录。", False),
        ("第五条 申请校级优秀学生奖学金，应当具备下列条件：", False),
        ("（一）学年平均学分绩点不低于 3.5；", False),
        ("（二）综合素质测评成绩位于本专业前 15%；", False),
        ("（三）学年内无纪律处分记录。", False),
    ],
    [
        ("第三章 评审程序与时间安排", True),
        ("第六条 奖学金评审按下列程序进行：", False),
        ("（一）学生本人提出申请，填写申请表并提交相关材料；", False),
        ("（二）学院学生工作办公室对申请材料进行初审；", False),
        ("（三）学院评审工作组组织评审并公示 3 个工作日；", False),
        ("（四）学校学生资助管理中心复核并在全校范围内公示 5 个工作日。", False),
        ("第七条 每学年秋季学期集中受理一次，申请材料提交截止时间为"
         "当年 9 月 10 日 17:00，逾期不再受理。", False),
        ("第八条 评审结果公示无异议后，奖学金于当年 11 月 30 日前发放至"
         "学生本人银行账户。", False),
    ],
    [
        ("第四章 申请材料与附则", True),
        ("第九条 申请国家励志奖学金应当提交下列材料：", False),
        ("（一）国家励志奖学金申请表（一式两份）；", False),
        ("（二）上一学年成绩单（须加盖学院公章）；", False),
        ("（三）家庭经济困难学生认定结果告知书；", False),
        ("（四）个人申请书（不少于 800 字）；", False),
        ("（五）获奖证书复印件（如有）。", False),
        ("第十条 申请人应当对所提交材料的真实性负责。"
         "经查实存在弄虚作假的，取消当学年及下一学年评奖资格。", False),
        ("第十一条 本办法由学生资助管理中心负责解释，自公布之日起施行。", False),
    ],
]

GUIDE_SECTIONS: list[tuple[str, str, list[str]]] = [
    ("一、事项范围", "本章说明本指南覆盖的常见学生事务类型。",
     ["在读证明办理", "成绩单打印", "学籍异动申请", "请假与销假"]),
    ("二、办理流程", "所有事项均通过线上服务大厅提交，线下窗口仅受理特殊情况。",
     ["登录服务大厅并完成实名认证",
      "选择事项类型并填写表单",
      "上传所需材料扫描件",
      "等待经办人审核（一般 2 个工作日）",
      "审核通过后到指定地点领取或线上下载"]),
    ("三、材料要求", "材料不齐是最常见的退回原因，提交前请逐项核对。",
     ["身份证件仅需核验，不留存复印件",
      "成绩单须为教务系统导出并带验证码",
      "委托他人办理须提供授权委托书"]),
    ("四、时限与费用", "常规事项免费办理，加急事项按学校规定执行。",
     ["在读证明：2 个工作日",
      "成绩单：即时生成",
      "学籍异动：5 个工作日"]),
]

GUIDE_TABLE = [
    ("事项", "办理时限", "是否收费"),
    ("在读证明", "2 个工作日", "免费"),
    ("成绩单打印", "即时", "免费"),
    ("学籍异动", "5 个工作日", "免费"),
    ("加急服务", "1 个工作日", "按规定收费"),
]


def _make_pdf(target: Path) -> None:
    import pymupdf

    doc = pymupdf.open()
    for page_no, lines in enumerate(PAGES, 1):
        page = doc.new_page()
        y = 72.0
        for text, is_heading in lines:
            size = 15 if is_heading else 10.5
            font = FONT
            # 简单换行：每 38 字折一行，保证不超出页面宽度
            for chunk in [text[i:i + 38] for i in range(0, len(text), 38)] or [""]:
                page.insert_text((64, y), chunk, fontname=font, fontsize=size)
                y += size + 8
            y += 6
        page.insert_text((500, 800), f"第 {page_no} 页 / 共 {len(PAGES)} 页",
                         fontname=FONT, fontsize=9)
    doc.save(target)
    doc.close()
    print(f"[ok] {target.relative_to(ROOT)}  {target.stat().st_size} 字节 / {len(PAGES)} 页")


def _make_scanned_pdf(target: Path) -> None:
    """先渲染成图片，再把图片贴进页面 —— 这样页面没有文本层，与真实扫描件一致。"""
    import pymupdf

    source = pymupdf.open()
    page = source.new_page()
    page.insert_text((72, 120), "示例大学 学生事务告知书（演示样例）",
                     fontname=FONT, fontsize=16)
    page.insert_text((72, 160), "本页为扫描件样例：内容以图片形式存在。",
                     fontname=FONT, fontsize=11)
    # dpi 72 + JPEG：保持"像扫描件"的同时把体积压到几十 KB，便于入库
    pix = page.get_pixmap(dpi=72)
    image_bytes = pix.tobytes("jpg")
    source.close()

    out = pymupdf.open()
    target_page = out.new_page()
    target_page.insert_image(target_page.rect, stream=image_bytes)
    out.save(target)
    out.close()

    # 自检：确认真的没有文本层
    check = pymupdf.open(target)
    chars = sum(len(check.load_page(i).get_text("text").strip())
                for i in range(check.page_count))
    check.close()
    print(f"[ok] {target.relative_to(ROOT)}  {target.stat().st_size} 字节 / 文本层 {chars} 字符")


def _make_docx(target: Path) -> None:
    import docx
    from docx.shared import Pt

    document = docx.Document()
    document.add_heading("示例大学学生事务办理指南（演示样例）", level=0)
    document.add_paragraph("本文件为演示用虚构材料，不指向任何真实机构。")

    for title, intro, items in GUIDE_SECTIONS:
        document.add_heading(title, level=1)
        document.add_paragraph(intro)
        for item in items:
            paragraph = document.add_paragraph(item, style="List Bullet")
            paragraph.paragraph_format.space_after = Pt(2)

    document.add_heading("五、办理时限一览", level=1)
    table = document.add_table(rows=len(GUIDE_TABLE), cols=3)
    table.style = "Table Grid"
    for row_index, row in enumerate(GUIDE_TABLE):
        for col_index, value in enumerate(row):
            table.cell(row_index, col_index).text = value

    document.add_heading("六、咨询方式", level=1)
    document.add_paragraph("学生服务大厅：工作日 8:30–17:00（演示信息）。")

    document.save(str(target))
    print(f"[ok] {target.relative_to(ROOT)}  {target.stat().st_size} 字节 "
          f"/ {len(document.paragraphs)} 段落")


def _make_empty_txt(target: Path) -> None:
    target.write_text("   \n\n\t\n   \n", encoding="utf-8")
    print(f"[ok] {target.relative_to(ROOT)}  {target.stat().st_size} 字节 / 仅空白")


def _make_fake_pdf(target: Path) -> None:
    """写一个 MZ 头（Windows 可执行文件特征）但扩展名为 .pdf —— 用于 T-01。"""
    target.write_bytes(b"MZ\x90\x00" + b"\x00" * 508 + b"This is not a real PDF.")
    print(f"[ok] {target.relative_to(ROOT)}  {target.stat().st_size} 字节 / 魔数=MZ")


def main() -> int:
    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    _make_pdf(DEMO_DIR / "01-本科生奖学金评审办法（演示样例）.pdf")
    _make_docx(DEMO_DIR / "02-学生事务办理指南（演示样例）.docx")
    _make_scanned_pdf(DEMO_DIR / "03-扫描件样例（演示样例）.pdf")
    _make_empty_txt(DEMO_DIR / "04-空文本样例（演示样例）.txt")
    _make_fake_pdf(DEMO_DIR / "05-改名的假文件（演示样例）.pdf")
    print("\n全部为虚构内容，可用于演示与自动化测试。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
