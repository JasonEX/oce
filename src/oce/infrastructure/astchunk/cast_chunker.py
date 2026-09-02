"""cAST chunker adapter with a configurable fallback.

The AST decides where chunk boundaries fall; the text of a chunk is always cut
from the source lines, so it lines up with the line numbers the formatter
prints.
"""

from __future__ import annotations

from loguru import logger

from oce.domain.chunk.lang import SUPPORTED_LANGUAGES, detect_language
from oce.domain.chunk.protocols import Chunker
from oce.domain.chunk.spans import (
    DEFAULT_MAX_CHUNK_CHARS,
    emit_chunks,
    is_meaningful,
    trim_trailing_blank_lines,
)
from oce.domain.chunk.types import Chunk
from oce.infrastructure.astchunk.astchunk_builder import (
    LANGUAGE_MAP,
    ASTChunkBuilder,
    AstWindow,
)

# Non-whitespace characters per character of source, at the low end. Measured
# over the supported grammars on the OpenClaw tree: Swift sits lowest at 0.67,
# Dockerfile highest at 0.88. Converting the character budget with the floor
# means the derived non-whitespace budget holds for every language, at the cost
# of leaving some headroom unused in the denser ones.
_MIN_NWS_DENSITY = 0.67

# Ranges holding less than this are folded into a neighbour. astchunk measures
# windows in non-whitespace characters and splits on column offsets, so a
# declaration whose body lands in the next window comes back as a line or two
# holding just a signature or a closing brace. Indexed on its own such a chunk
# matches a name but answers nothing, and it takes a retrieval slot from the
# body that does.
DEFAULT_MIN_CHUNK_CHARS = 300


class CastChunker:
    """AST implementation for programming languages with semantic parsers.

    Languages with a dedicated chunker (markdown, jsp, vue, svelte) are excluded
    here. Files whose grammar cannot be loaded, or that resolve to no window,
    go to ``fallback``.
    """

    _EXCLUDED_LANGUAGES = frozenset({"markdown", "jsp", "vue", "svelte"})
    languages = frozenset(
        SUPPORTED_LANGUAGES.intersection(LANGUAGE_MAP) - _EXCLUDED_LANGUAGES
    )

    def __init__(
        self,
        *,
        max_chunk_size: int,
        fallback: Chunker,
        max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS,
        min_chunk_chars: int = DEFAULT_MIN_CHUNK_CHARS,
    ):
        if max_chunk_size <= 0:
            raise ValueError("max_chunk_size 必须 > 0")
        if max_chunk_chars <= 0:
            raise ValueError("max_chunk_chars 必须 > 0")
        if min_chunk_chars < 0 or min_chunk_chars >= max_chunk_chars:
            raise ValueError("min_chunk_chars 必须 ∈ [0, max_chunk_chars)")
        self.max_chunk_size = max_chunk_size
        self.fallback = fallback
        self.max_chunk_chars = max_chunk_chars
        self.min_chunk_chars = min_chunk_chars
        self._builders: dict[str, ASTChunkBuilder | None] = {}

    def chunk(self, content: str, path: str) -> list[Chunk]:
        if not is_meaningful(content):
            return []
        language = detect_language(path)
        builder = self._get_builder(language) if language is not None else None
        if builder is None:
            return self.fallback.chunk(content, path)
        return self._chunk_ast(builder, content, path)

    def _chunk_ast(
        self, builder: ASTChunkBuilder, content: str, path: str
    ) -> list[Chunk]:
        windows = builder.chunkify(content)
        lines = content.splitlines()
        ranges = [self._resolve_range(window, lines) for window in windows]
        chunks = emit_chunks(
            ((start, end, "ast") for start, end in self._merge_small(ranges, lines)),
            lines,
            path,
            max_chars=self.max_chunk_chars,
        )
        if chunks:
            return chunks
        # 解析成功但所有行都超出字符预算：文件是压缩包或单行生成产物。
        # 回退到 RecursiveChunker 只会把同样的内容按字符切回来，所以不产出。
        if windows:
            return []
        return self.fallback.chunk(content, path)

    def _merge_small(
        self,
        ranges: list[tuple[int, int]],
        lines: list[str],
    ) -> list[tuple[int, int]]:
        """Fold undersized ranges into a neighbour, keeping coverage in order.

        astchunk measures a window in non-whitespace characters and cuts on
        column offsets, so one source construct can come back as two windows
        starting on the same line: the first holds only what precedes the split
        column, the second the rest. Both then resolve to line ranges where the
        first is a prefix of the second, and emitting it separately indexes a
        bare signature.

        A range is absorbed when it is already covered by what has been kept, or
        when its text is below the floor. Absorption extends the previous kept
        range instead of dropping lines, so the union of the output still covers
        the union of the input.
        """
        if self.min_chunk_chars == 0:
            return ranges
        merged: list[tuple[int, int]] = []
        for start, end in sorted(ranges):
            if merged:
                prev_start, prev_end = merged[-1]
                # 已被前一个区间覆盖：重复起点或完全内含，不产出新块。
                if end <= prev_end:
                    continue
                # 前一个区间还没达到下限，把当前区间并进去补足它。
                if self._span_chars(lines, prev_start, prev_end) < self.min_chunk_chars:
                    merged[-1] = (prev_start, end)
                    continue
            if merged and self._span_chars(lines, start, end) < self.min_chunk_chars:
                # 自身过小且前一个已达标：向前贴，避免留下孤立的收尾括号。
                merged[-1] = (merged[-1][0], end)
                continue
            merged.append((start, end))
        return merged

    @staticmethod
    def _span_chars(lines: list[str], start: int, end: int) -> int:
        """Character count of a 1-based inclusive line range, newlines included."""
        return sum(len(lines[row]) + 1 for row in range(start - 1, end)) - 1

    @staticmethod
    def _resolve_range(window: AstWindow, lines: list[str]) -> tuple[int, int]:
        """Convert a window's 0-based rows into a 1-based inclusive line range.

        The window reports the row of its last byte. When it ends at column 0
        it stopped at the line break, so that row belongs to the next chunk;
        otherwise the row is part of this one. Trailing blank lines are dropped
        because joined text would never reach them.
        """
        start = window.start_row + 1
        if window.end_column == 0 and window.end_row > window.start_row:
            end = window.end_row
        else:
            end = window.end_row + 1
        line_count = len(lines)
        if start < 1 or end < start or end > line_count:
            raise ValueError(
                f"astchunk 返回无效行号: {start}-{end}，文件共 {line_count} 行"
            )
        return start, trim_trailing_blank_lines(lines, start, end)

    def _get_builder(self, language: str) -> ASTChunkBuilder | None:
        """One parser per language; a grammar that fails to load routes to fallback."""
        if language not in self._builders:
            try:
                self._builders[language] = ASTChunkBuilder(
                    max_chunk_size=self.max_chunk_size,
                    language=language,
                    intact_node_size=self._intact_node_size(),
                )
            except Exception as exc:
                logger.warning(
                    "tree-sitter grammar unavailable for {}; using text fallback: {}",
                    language,
                    exc,
                )
                self._builders[language] = None
        return self._builders[language]

    def _intact_node_size(self) -> int:
        """Largest declaration, in non-whitespace characters, kept whole.

        The two budgets measure different things: the builder counts
        non-whitespace characters, ``cap_span`` counts every character. Deriving
        one from the other keeps them from working against each other — a
        declaration held together by the builder only to be cut by ``cap_span``
        ends up in the same broken halves the intact rule exists to avoid.

        ``_MIN_NWS_DENSITY`` is the floor measured across the supported
        languages (Swift indents most heavily, at 0.67); using the floor rather
        than a per-language figure means the bound holds before any of the file
        has been read.

        The window size is the lower bound because a ceiling below it would
        reject nodes the builder is willing to place whole anyway, which is not
        this rule's business.
        """
        derived = int(self.max_chunk_chars * _MIN_NWS_DENSITY)
        return max(self.max_chunk_size, derived)
