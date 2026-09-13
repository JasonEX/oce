"""RecursiveChunker: the fallback over LangChain's RecursiveCharacterTextSplitter.

Used for files whose language is unknown and when a dedicated chunker fails.

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
    """Generic chunker over ``RecursiveCharacterTextSplitter``."""

    def __init__(
        self,
        chunk_size: int = DEFAULT_MAX_CHUNK_CHARS,
        chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    ):
        """``chunk_overlap`` only moves the split points; chunks never overlap.

        Repeated text harms line-numbered chunks: two chunks claiming the same
        lines index one piece of source twice and occupy two result slots.
        """
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if chunk_overlap < 0 or chunk_overlap >= chunk_size:
            raise ValueError("chunk_overlap must be in [0, chunk_size)")
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    def chunk(self, content: str, path: str) -> list[Chunk]:
        if not is_meaningful(content):
            return []
        lines = content.splitlines()
        if not lines:
            return []

        splitter = self._create_splitter(detect_language(path))
        # create_documents, not split_text: only it carries start_index, the
        # one reliable way to map a split back to a line. Searching the text
        # with find() lands on the wrong line once the splitter has stripped
        # whitespace.
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
            # start_index comes from the splitter's own str.find and is -1 when
            # not found. Either way the line is unknown, and one cut fewer is
            # better than a wrong position.
            if position is None or position < 0:
                continue
            starts.append(line_of(offsets, min(position, len(content))))
        return starts or [1]
