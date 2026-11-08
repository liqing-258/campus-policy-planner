"""文档解析：PDF / DOCX / TXT / MD → 结构化文本块。

设计要点（对应系统设计说明书 §4.2 与 T-03/T-04/T-05）：

1. **绝不静默失败**。加密、损坏、无文本层都必须返回可读原因，
   因为"解析成功但空"比"解析失败"更危险——用户会以为文件里没有这条政策。
2. **保留定位信息**。PDF 保留页码，DOCX/TXT 保留章节标题，
   这是"每条结论都能点回原文"的前提。
3. **不做 OCR**。无文本层就明确提示"疑似扫描件"，不猜、不编。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# 解析状态（与 shared/enums.py 保持一致，此处只引用字符串常量）
STATUS_PARSED = "parsed"
STATUS_EMPTY = "empty"
STATUS_FAILED = "failed"


class ParseError(Exception):
    """解析失败。message 会直接展示给用户，因此必须是中文、不含堆栈。"""

    def __init__(self, message: str, kind: str = "error") -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind


@dataclass
class Block:
    """一个语义块（段落 / 表格行 / 标题下的正文）。"""

    text: str
    page_number: int | None
    section_title: str | None
    char_start: int
    char_end: int


@dataclass
class ParseOutcome:
    status: str
    blocks: list[Block] = field(default_factory=list)
    full_text: str = ""
    page_count: int | None = None
    error: str | None = None
    hint: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def char_count(self) -> int:
        return len(self.full_text)


# ---------------------------------------------------------------- 标题识别
# 只认"章节级"编号，**不认"条款级"编号**。
#
# 第一版把「第X条」也当标题，结果 949 字的文件被切成 33 个 6 字片段：
# 因为「第一条 为规范……制定本办法。」整段都以「第一条」开头，
# 一旦当标题，每段都成了独立章节，切分器再无内容可合并。
# 条款编号属于正文，必须留在正文里——它本身就是"哪一条"的定位信息。
_STRONG_HEADING = (
    re.compile(r"^第[一二三四五六七八九十百零〇\d]+[章编]"),
    re.compile(r"^第[一二三四五六七八九十百零〇\d]+部分"),
    re.compile(r"^[一二三四五六七八九十]+、"),
    re.compile(r"^附件\s*\d*"),
)
# 这些只是一段话的开头，仅用于日志/调试，不构成章节边界
_WEAK_HEADING = (
    re.compile(r"^第[一二三四五六七八九十百零〇\d]+条"),
    re.compile(r"^[（(][一二三四五六七八九十]+[）)]"),
    re.compile(r"^\d+(\.\d+){0,2}[\s、．.]"),
)
_PAGE_NOISE = re.compile(r"^\s*[-—–]?\s*\d{1,3}\s*[-—–]?\s*$")

# 章节标题必须短，且不能以句末标点收尾——否则是"以编号开头的整段正文"
MAX_SECTION_HEADING_LEN = 30
_SENTENCE_ENDINGS = ("。", "；", "：", "，", ",")


def is_section_heading(line: str) -> bool:
    text = line.strip()
    if not text or len(text) > MAX_SECTION_HEADING_LEN:
        return False
    if text.endswith(_SENTENCE_ENDINGS):
        return False
    return any(p.match(text) for p in _STRONG_HEADING)


def is_clause_start(line: str) -> bool:
    """条款开头（第X条 / （一） / 1.）。仅用于统计，不作为章节边界。"""
    text = line.strip()
    return bool(text) and any(p.match(text) for p in _WEAK_HEADING)


def _clean_lines(raw: str) -> list[str]:
    lines = []
    for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = line.strip()
        if _PAGE_NOISE.match(stripped):
            continue  # 页眉页脚里的孤立页码
        lines.append(stripped)
    return lines


class _Builder:
    """按顺序累积块并维护全文偏移量。"""

    def __init__(self) -> None:
        self.blocks: list[Block] = []
        self._parts: list[str] = []
        self._cursor = 0
        self._section: str | None = None
        self.warnings: list[str] = []

    @property
    def full_text(self) -> str:
        return "".join(self._parts)

    def add(self, text: str, page_number: int | None, *,
            is_heading: bool = False) -> None:
        text = text.strip()
        if not text:
            return
        if is_heading:
            self._section = text[:200]
        start = self._cursor
        self._parts.append(text + "\n")
        self._cursor = start + len(text) + 1
        self.blocks.append(Block(
            text=text,
            page_number=page_number,
            section_title=self._section,
            char_start=start,
            char_end=start + len(text),
        ))


# ---------------------------------------------------------------- PDF
def _parse_pdf(path: Path, builder: _Builder) -> int:
    import pymupdf

    try:
        doc = pymupdf.open(path)
    except Exception as exc:
        raise ParseError(f"PDF 无法打开，文件可能已损坏：{exc}", kind="corrupt")

    try:
        if doc.needs_pass:
            raise ParseError("PDF 已加密，无法读取正文。请提供解密后的文件。", kind="encrypted")
        page_count = doc.page_count
        if page_count == 0:
            raise ParseError("PDF 没有任何页面。", kind="corrupt")

        for index in range(page_count):
            page = doc.load_page(index)
            raw = page.get_text("text") or ""
            for line in _clean_lines(raw):
                builder.add(line, page_number=index + 1,
                            is_heading=is_section_heading(line))
    finally:
        doc.close()
    return page_count


# ---------------------------------------------------------------- DOCX
def _parse_docx(path: Path, builder: _Builder) -> int | None:
    try:
        import docx
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        document = docx.Document(str(path))
    except Exception as exc:
        raise ParseError(f"DOCX 无法打开，文件可能已损坏：{exc}", kind="corrupt")

    for item in document.iter_inner_content():
        if isinstance(item, Paragraph):
            text = item.text.strip()
            if not text:
                continue
            style = (item.style.name or "").lower() if item.style is not None else ""
            # 只有一级标题算章节边界；Heading 2/3 只是排版层级，归入正文，
            # 否则"（一）…"这类子标题会把同一章切碎
            is_heading = (
                style in ("heading 1", "标题 1", "title", "标题")
                or is_section_heading(text)
            )
            builder.add(text, page_number=None, is_heading=is_heading)
        elif isinstance(item, Table):
            for row in item.rows:
                cells = [c.text.strip().replace("\n", " ") for c in row.cells]
                if any(cells):
                    builder.add(" | ".join(cells), page_number=None)
    return None


# ---------------------------------------------------------------- TXT / MD
def _decode(raw: bytes) -> tuple[str, str]:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8(含替换字符)"


def _parse_plain(path: Path, builder: _Builder, *, is_markdown: bool) -> int | None:
    raw = path.read_bytes()
    text, encoding = _decode(raw)
    if "替换字符" in encoding:
        builder.warnings.append("文件编码无法识别，已用替换字符兜底，部分内容可能丢失")

    for line in _clean_lines(text):
        if not line:
            continue
        is_heading = False
        if is_markdown and line.startswith("#"):
            is_heading = True
            line = line.lstrip("#").strip()
        elif is_markdown and line.startswith(("- ", "* ", "+ ")):
            line = line[2:].strip()
        else:
            is_heading = is_section_heading(line)
        builder.add(line, page_number=None, is_heading=is_heading)
    return None


# ---------------------------------------------------------------- 统一入口
PARSERS = {
    "pdf": _parse_pdf,
    "docx": _parse_docx,
}


def parse_document(path: str | Path, file_type: str, *,
                   min_text_chars: int = 50,
                   scanned_page_char_threshold: int = 20) -> ParseOutcome:
    """解析文档。任何已知异常都转成 ParseOutcome(status=failed/empty)，不向上抛。"""
    path = Path(path)
    if not path.exists():
        return ParseOutcome(status=STATUS_FAILED, error="文件不存在，可能已被清理")

    builder = _Builder()
    page_count: int | None = None
    try:
        if file_type == "pdf":
            page_count = _parse_pdf(path, builder)
        elif file_type == "docx":
            page_count = _parse_docx(path, builder)
        elif file_type in ("txt", "md"):
            page_count = _parse_plain(path, builder, is_markdown=(file_type == "md"))
        else:
            return ParseOutcome(status=STATUS_FAILED,
                                error=f"不支持的文件类型：{file_type}")
    except ParseError as exc:
        return ParseOutcome(status=STATUS_FAILED, error=exc.message, page_count=page_count)
    except Exception as exc:  # 兜底：解析器内部未预料的错误也不允许 500
        return ParseOutcome(
            status=STATUS_FAILED,
            error=f"解析过程出现未预期错误（{type(exc).__name__}: {exc}）",
        )

    full_text = builder.full_text
    outcome = ParseOutcome(
        status=STATUS_PARSED,
        blocks=builder.blocks,
        full_text=full_text,
        page_count=page_count,
        warnings=list(builder.warnings),
    )

    char_count = len(full_text.strip())
    if char_count < min_text_chars:
        outcome.status = STATUS_EMPTY
        if file_type == "pdf" and page_count:
            avg = char_count / max(page_count, 1)
            if avg < scanned_page_char_threshold:
                outcome.hint = (
                    f"共 {page_count} 页，仅提取到 {char_count} 个字符（平均每页 "
                    f"{avg:.1f} 字），疑似扫描件。本系统不含 OCR，"
                    "请上传带文本层的 PDF 或手工录入。"
                )
            else:
                outcome.hint = f"共 {page_count} 页，仅提取到 {char_count} 个字符，内容过少。"
        else:
            outcome.hint = f"仅提取到 {char_count} 个字符，低于有效阈值 {min_text_chars}。"
        outcome.error = outcome.hint
    return outcome
