"""Formatter — 拼装 formatted_retrieval。

目标输出形态（来自接口约定）
----------------------------
    The following code sections were retrieved:
    Path: main.py
    Lines: 1-3
         1\txxx
         2\t...

职责
----
- chunk 原文直接取自 SearchHit.content（已随检索从存储 JOIN 出来）。
- 加行号、按 Path + 行号区间标注，每个 hit 独立片段，保留 score 排序。
- 带封闭作用域的片段多一行 ``Context:``，让读者知道方法属于哪个类。
- role=related 的定义摘要单独成节，放在主结果之后，明确标注只是签名级摘录。
"""

from __future__ import annotations

from oce.domain.services.search import SearchHit

HEADER = "The following code sections were retrieved:"
RELATED_HEADER = (
    "Related definitions referenced by the sections above (signature excerpts):"
)


def _section(hit: SearchHit) -> str:
    lines = hit.content.splitlines()
    formatted_lines = [
        f"{hit.start_line + offset:>6}\t{line_content}"
        for offset, line_content in enumerate(lines)
    ]
    header = f"Path: {hit.path}\nLines: {hit.start_line}-{hit.end_line}\n"
    if hit.context:
        header += f"Context: {hit.context}\n"
    return header + "\n".join(formatted_lines)


def format_retrieval(hits: list[SearchHit]) -> str:
    """拼装带行号的检索结果文本。

    每个 hit 独立渲染为一个片段（不合并同文件的多个 chunk），保留 score 排序顺序。
    行号从 hit.start_line 起，逐行配 SearchHit.content 的内容。
    """
    primary = [_section(hit) for hit in hits if hit.role == "primary"]
    related = [_section(hit) for hit in hits if hit.role == "related"]

    if not primary and not related:
        return HEADER
    text = HEADER
    if primary:
        text += "\n" + "\n\n".join(primary)
    if related:
        text += "\n\n" + RELATED_HEADER + "\n" + "\n\n".join(related)
    return text
