"""结构化切分：先按章节，再按段落，最后按句子，带重叠。

为什么不用"固定长度硬切"：政策文件的价值在于条款边界。
硬切会把"（三）家庭经济困难"和它的认定标准切成两段，
检索命中了前半段却拿不到判定依据——这正是"引用看起来有，但答不出来"的根源。

切分策略（自上而下，能整就不拆）：
    章节标题边界 → 段落 → 句子（。！？；换行）→ 硬切（兜底）

重叠只发生在**同章节内被强制拆开**的相邻片段之间，跨章节不重叠。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.parsing import Block

# 句子边界：中文句末标点与换行
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？；!?;])\s*")


@dataclass
class Chunk:
    chunk_index: int
    content: str
    char_start: int
    char_end: int
    page_number: int | None
    section_title: str | None
    token_count: int


def estimate_tokens(text: str) -> int:
    """粗估 token：中文按 1 字 ≈ 0.7 token，英文按 ~4 字符 ≈ 1 token。

    只用于展示与容量控制，不用于计费。公式写死在这里，
    避免各模块各算一套导致"片段长度"统计互相对不上。
    """
    if not text:
        return 0
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    other = len(text) - cjk
    return int(cjk * 0.7 + other / 4) or 1


def _split_sentences(text: str) -> list[str]:
    parts = [p for p in _SENTENCE_SPLIT.split(text) if p]
    return parts or [text]


def _hard_split(text: str, size: int, overlap: int) -> list[str]:
    """兜底：单句仍超长（表格行、无标点长串）时按字符窗口切。"""
    step = max(size - overlap, 1)
    return [text[i:i + size] for i in range(0, len(text), step)]


def _pack(units: list[tuple[str, int, int]], size: int, overlap: int) -> list[tuple[str, int, int]]:
    """把句子装进不超过 size 的窗口，返回 (内容, 起, 止)。

    `dirty` 标记"窗口里是否有尚未输出的内容"。没有它，窗口刚好装满后再 flush
    会把保留下来的 overlap 前缀当成新片段再输出一次，产生重复片段。
    """
    packed: list[tuple[str, int, int]] = []
    buffer: list[tuple[str, int, int]] = []
    length = 0
    dirty = False

    for unit in units:
        if buffer and length + len(unit[0]) > size:
            packed.append(("".join(u[0] for u in buffer), buffer[0][1], buffer[-1][2]))
            kept: list[tuple[str, int, int]] = []
            kept_len = 0
            for prev in reversed(buffer):
                if kept_len + len(prev[0]) > overlap:
                    break
                kept.insert(0, prev)
                kept_len += len(prev[0])
            buffer, length, dirty = kept, kept_len, False
        buffer.append(unit)
        length += len(unit[0])
        dirty = True

    if dirty and buffer:
        packed.append(("".join(u[0] for u in buffer), buffer[0][1], buffer[-1][2]))
    return packed


def _merge_small_sections(sections: list[tuple[str | None, list[Block]]],
                          min_chars: int) -> list[tuple[str | None, list[Block]]]:
    """把"只有一行很短内容"的章节并入下一节（末尾则并入上一节）。

    典型场景：文档标题单独占一节（"XX办法（演示样例）"），
    若不为它找归宿，就会产出一个 20 字的无意义片段，被检索命中后毫无价值。

    阈值刻意取得很低（默认 40 字）：合并章节会丢掉章节标题这个检索线索，
    宁可多几个小片段，也不要把"第一章"和"第二章"揉进同一个片段。
    """
    merged: list[tuple[str | None, list[Block]]] = []
    carry: list[Block] = []

    for title, blocks in sections:
        if sum(len(b.text) for b in blocks) < min_chars:
            carry.extend(blocks)
            continue
        merged.append((title, carry + blocks if carry else blocks))
        carry = []

    if carry:
        if merged:
            title, blocks = merged[-1]
            merged[-1] = (title, blocks + carry)
        else:
            merged.append((sections[0][0] if sections else None, carry))
    return merged


def chunk_blocks(blocks: list[Block], *, chunk_size: int = 600,
                 chunk_overlap: int = 80,
                 min_section_chars: int = 40) -> list[Chunk]:
    """把解析块切分成片段。输入为空返回空列表（不是空片段）。"""
    if not blocks:
        return []

    size = max(chunk_size, 100)
    overlap = max(0, min(chunk_overlap, size // 2))

    # 1) 按章节分组，章节内保持原始顺序
    sections: list[tuple[str | None, list[Block]]] = []
    for block in blocks:
        if sections and sections[-1][0] == block.section_title:
            sections[-1][1].append(block)
        else:
            sections.append((block.section_title, [block]))
    sections = _merge_small_sections(sections, min_section_chars)

    chunks: list[Chunk] = []
    index = 0
    for section_title, section_blocks in sections:
        # 2) 段落合并：短段落先拼在一起，避免"一句话一个片段"
        merged: list[tuple[str, int, int]] = []
        for block in section_blocks:
            if merged and len(merged[-1][0]) + len(block.text) + 1 <= size // 2:
                prev_text, prev_start, _ = merged[-1]
                merged[-1] = (prev_text + "\n" + block.text, prev_start,
                              block.char_end)
            else:
                merged.append((block.text, block.char_start, block.char_end))

        # 3) 超长段落按句子拆，再装箱
        units: list[tuple[str, int, int]] = []
        for text, start, end in merged:
            if len(text) <= size:
                units.append((text, start, end))
                continue
            cursor = start
            for sentence in _split_sentences(text):
                if len(sentence) > size:
                    for piece in _hard_split(sentence, size, overlap):
                        units.append((piece, cursor, cursor + len(piece)))
                        cursor += len(piece)
                else:
                    units.append((sentence, cursor, cursor + len(sentence)))
                    cursor += len(sentence)

        for content, start, end in _pack(units, size, overlap):
            content = content.strip()
            if not content:
                continue
            chunks.append(Chunk(
                chunk_index=index,
                content=content,
                char_start=start,
                char_end=end,
                page_number=_page_of(section_blocks, start),
                section_title=section_title,
                token_count=estimate_tokens(content),
            ))
            index += 1
    return chunks


def _page_of(blocks: list[Block], offset: int) -> int | None:
    """片段跨页时取起始页（用户是从这一页开始读的）。"""
    for block in blocks:
        if block.char_start <= offset <= block.char_end:
            return block.page_number
    return blocks[0].page_number if blocks else None
