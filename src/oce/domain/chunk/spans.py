"""Line-span helpers shared by the chunkers.

A span is a 1-based inclusive line range paired with the exact text of those
lines. Keeping text and range in one value makes the invariant checkable: a
chunk must render at the line numbers it claims, because the formatter prints
``start_line + offset`` for each of its lines.

Capping keeps chunk text under a character budget. Without it the embedding
client re-splits long inputs and pools the pieces, and the vector store
truncates its text field, so a chunk would be indexed under content nobody can
reconstruct from the reported line range.

A line longer than the whole budget cannot be capped without breaking that
invariant, so it is dropped instead. Such lines are minified bundles or
generated single-line payloads; a mid-token slice of one is not something a
reader can act on, and emitting several pieces under the same line number makes
every one of them misreport its source range.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from oce.domain.chunk.types import Chunk

# Keeps chunk text inside the embedding client's per-input window and the vector
# store's text field. Above it the client re-splits and pools the pieces and the
# store truncates, so the indexed text stops matching the reported line range.
DEFAULT_MAX_CHUNK_CHARS = 6_000

# (start_line, end_line, text) with 1-based inclusive line numbers.
Span = tuple[int, int, str]

# (start_line, end_line, chunk_type) handed to ``emit_chunks``.
TypedRange = tuple[int, int, str]


def is_meaningful(text: str | bytes) -> bool:
    """是否含有效信息（至少一个字母/数字）。"""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="ignore")
    return any(ch.isalnum() for ch in text)


def slice_lines(lines: list[str], start_line: int, end_line: int) -> str:
    """Return the verbatim text of a 1-based inclusive line range."""
    return "\n".join(lines[start_line - 1 : end_line])


def trim_trailing_blank_lines(lines: list[str], start_line: int, end_line: int) -> int:
    """Pull ``end_line`` back over trailing blank lines.

    ``"\\n".join`` drops the final empty element, so a range ending on blank
    lines yields text with fewer lines than the range claims.
    """
    while end_line > start_line and not lines[end_line - 1].strip():
        end_line -= 1
    return end_line


def line_offsets(lines: list[str]) -> list[int]:
    """Character offset of each line's first character."""
    offsets: list[int] = []
    position = 0
    for line in lines:
        offsets.append(position)
        position += len(line) + 1
    return offsets


def line_of(offsets: list[int], position: int) -> int:
    """Binary-search the 1-based line owning a character offset."""
    low, high = 0, len(offsets) - 1
    while low < high:
        mid = (low + high + 1) // 2
        if offsets[mid] <= position:
            low = mid
        else:
            high = mid - 1
    return low + 1


def tile_spans(
    starts: Iterable[int],
    total_lines: int,
    *,
    fold_preamble: bool,
) -> list[tuple[int, int]]:
    """Turn start lines into contiguous, non-overlapping line ranges.

    Each range runs until the line before the next start. Lines above the first
    start are covered either by folding them into the first range
    (``fold_preamble=True``, so a heading opens the chunk it belongs to) or by
    emitting them as their own range.
    """
    ordered = sorted({start for start in starts if 1 <= start <= total_lines})
    if not ordered:
        ordered = [1]
    if ordered[0] != 1:
        if fold_preamble:
            ordered[0] = 1
        else:
            ordered.insert(0, 1)
    spans: list[tuple[int, int]] = []
    for index, start in enumerate(ordered):
        end = ordered[index + 1] - 1 if index + 1 < len(ordered) else total_lines
        if end >= start:
            spans.append((start, end))
    return spans


def cap_span(
    lines: list[str],
    start_line: int,
    end_line: int,
    max_chars: int,
) -> list[Span]:
    """Split one line range into spans whose text fits ``max_chars``.

    Splits on line boundaries only. Lines that exceed the budget on their own
    are skipped, so every returned span's text equals the source lines it
    claims. Skipping one breaks the buffer, which is why the surrounding lines
    come back as separate spans rather than one range straddling the gap.
    """
    if max_chars < 1:
        raise ValueError("max_chars must be positive")

    spans: list[Span] = []
    buffer: list[str] = []
    current_start = start_line
    length = 0

    for line_no in range(start_line, end_line + 1):
        line = lines[line_no - 1]
        if len(line) > max_chars:
            if buffer:
                spans.append((current_start, line_no - 1, "\n".join(buffer)))
                buffer = []
                length = 0
            current_start = line_no + 1
            continue
        addition = len(line) + (1 if buffer else 0)
        if buffer and length + addition > max_chars:
            spans.append((current_start, line_no - 1, "\n".join(buffer)))
            buffer = []
            length = 0
            current_start = line_no
            addition = len(line)
        buffer.append(line)
        length += addition

    if buffer:
        spans.append((current_start, end_line, "\n".join(buffer)))
    return spans


def _has_text(text: str) -> bool:
    return bool(text.strip())


def emit_chunks(
    ranges: Iterable[TypedRange],
    lines: list[str],
    path: str,
    *,
    max_chars: int,
    keep: Callable[[str], bool] = _has_text,
) -> list[Chunk]:
    """Cut verbatim chunks for typed line ranges: trim, cap, then hash.

    ``keep`` decides whether a capped piece is worth indexing; the default drops
    whitespace-only text.
    """
    chunks: list[Chunk] = []
    for start, end, chunk_type in ranges:
        trimmed = trim_trailing_blank_lines(lines, start, end)
        for span_start, span_end, text in cap_span(lines, start, trimmed, max_chars):
            if not keep(text):
                continue
            chunks.append(
                Chunk(
                    content_hash=Chunk.compute_hash(text),
                    path=path,
                    content=text,
                    start_line=span_start,
                    end_line=span_end,
                    chunk_type=chunk_type,
                )
            )
    return chunks
