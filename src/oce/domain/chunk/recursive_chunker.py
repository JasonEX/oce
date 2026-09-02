"""RecursiveChunker — 基于 LangChain RecursiveCharacterTextSplitter 的兜底切块器。

用于无法识别语言的文件，以及各专用 chunker 解析失败时的 fallback。

The splitter decides where boundaries fall; the text of a chunk is always cut
from the source lines, the same contract ``CastChunker`` and ``MarkdownChunker``
follow. Its own output cannot serve as chunk text: it strips whitespace at every
separator, so the returned string no longer matches the lines it came from and
the line number derived from it points at the wrong place. Measured on the
OpenClaw tree, 13.3% of the chunks produced that way reported a range whose
source text differed from the chunk itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from oce.domain.chunk.lang import detect_language
from oce.domain.chunk.spans import (
    DEFAULT_MAX_CHUNK_CHARS,
    emit_chunks,
    is_meaningful,
    line_of,
    line_offsets,
    tile_spans,
)
from oce.domain.chunk.types import Chunk

if TYPE_CHECKING:
    from langchain_text_splitters import RecursiveCharacterTextSplitter

DEFAULT_CHUNK_OVERLAP = 200


class RecursiveChunker:
    """基于 LangChain RecursiveCharacterTextSplitter 的通用 Chunker。"""

    def __init__(
        self,
        chunk_size: int = DEFAULT_MAX_CHUNK_CHARS,
        chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    ):
        """``chunk_overlap`` 只影响 splitter 在何处切分，不产生块间重叠。

        重复的文本对带行号的 chunk 是有害的：两个块声明同样的行，就会把同一段
        源码索引两遍，并占掉两个检索位。
        """
        if chunk_size <= 0:
            raise ValueError("chunk_size 必须 > 0")
        if chunk_overlap < 0 or chunk_overlap >= chunk_size:
            raise ValueError("chunk_overlap 必须 ∈ [0, chunk_size)")
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    def chunk(self, content: str, path: str) -> list[Chunk]:
        if not is_meaningful(content):
            return []
        lines = content.splitlines()
        if not lines:
            return []

        splitter = self._create_splitter(detect_language(path))
        # create_documents 而不是 split_text：只有它会带上 start_index，而起始
        # 偏移是把切点映射回行号的唯一可靠依据。用 find() 反查文本会在 splitter
        # strip 掉空白后落到错误的行上。
        try:
            pieces = splitter.create_documents([content])
        except Exception as error:
            logger.debug(
                "RecursiveCharacterTextSplitter failed for {}: {}", path, error
            )
            return []
        starts = self._start_lines(content, lines, pieces)
        spans = tile_spans(starts, len(lines), fold_preamble=False)
        # ``chunk_size`` says how large a chunk should be; the cap says how large
        # one may be before the embedding client re-splits it. A caller asking
        # for small chunks must not lose ordinary long lines to the cap.
        return emit_chunks(
            ((start, end, "recursive") for start, end in spans),
            lines,
            path,
            max_chars=max(self.chunk_size, DEFAULT_MAX_CHUNK_CHARS),
            keep=is_meaningful,
        )

    def _create_splitter(self, language: str | None) -> RecursiveCharacterTextSplitter:
        from langchain_text_splitters import RecursiveCharacterTextSplitter
        from langchain_text_splitters.base import Language

        lang_map = {
            "python": Language.PYTHON,
            "javascript": Language.JS,
            "typescript": Language.TS,
            "java": Language.JAVA,
            "cpp": Language.CPP,
            "go": Language.GO,
            "rust": Language.RUST,
            "markdown": Language.MARKDOWN,
            "html": Language.HTML,
        }
        lang_enum = lang_map.get(language) if language else None
        if lang_enum is not None:
            return RecursiveCharacterTextSplitter.from_language(
                language=lang_enum,
                chunk_size=self.chunk_size,
                chunk_overlap=self.chunk_overlap,
                add_start_index=True,
            )
        return RecursiveCharacterTextSplitter(
            chunk_size=self.chunk_size,
            chunk_overlap=self.chunk_overlap,
            separators=["\n\n", "\n", " ", ""],
            add_start_index=True,
        )

    @staticmethod
    def _start_lines(content: str, lines: list[str], pieces: list) -> list[int]:
        """Map each piece's start offset onto the 1-based line that owns it.

        A boundary landing mid-line is pulled back to that line's start: a chunk
        may not begin halfway through a line, or its text would no longer be the
        lines it claims. Splitting mid-line only happens once the splitter has
        exhausted its line-based separators, on input with no line structure
        left to respect.
        """
        offsets = line_offsets(lines)
        starts: list[int] = []
        for piece in pieces:
            position = piece.metadata.get("start_index")
            # start_index 来自 splitter 内部的 str.find，找不到时是 -1。缺失或
            # 找不到都意味着无从确定行号，此时宁可少切一刀，也不能报错的位置。
            if position is None or position < 0:
                continue
            starts.append(line_of(offsets, min(position, len(content))))
        return starts or [1]
