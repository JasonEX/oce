"""Relation evidence: who calls, tests, or re-exports the requested symbols.

The exact lane answers "where is X declared"; these lookups answer the next
questions an editing task asks and are appended to the result as separate,
individually budgeted sections. Each is one SQL lookup over
``symbol_occurrences`` and returns chunk-level hits with the occurrence line
and the enclosing definition, so the pipeline can cut a signature-sized
excerpt instead of pasting whole chunks.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Protocol

from oce.domain.services.search import HitRole, SearchHit, SearchScope


@dataclass(frozen=True)
class RelatedOccurrence:
    """One occurrence of a requested identifier inside an indexed chunk."""

    identifier: str
    kind: str
    hit: SearchHit
    line: int
    end_line: int
    # Innermost definition holding the occurrence; empty at module level.
    enclosing: str = ""
    hop: int | None = None


class RelationStore(Protocol):
    async def find_callers(
        self,
        *,
        identifiers: Sequence[str],
        scope: SearchScope,
        limit: int = 8,
    ) -> list[RelatedOccurrence]:
        """Call sites grouped one per (file, enclosing definition), in path order."""
        ...

    async def find_test_uses(
        self,
        *,
        identifiers: Sequence[str],
        scope: SearchScope,
        limit: int = 8,
    ) -> list[RelatedOccurrence]:
        """Occurrences inside test files, one per (file, enclosing test)."""
        ...

    async def find_implementations(
        self,
        *,
        identifiers: Sequence[str],
        scope: SearchScope,
        limit: int = 8,
    ) -> list[RelatedOccurrence]:
        """Declarations that extend or implement the identifiers (``inherit`` rows)."""
        ...

    async def find_reexports(
        self,
        *,
        identifiers: Sequence[str],
        scope: SearchScope,
        limit: int = 4,
    ) -> list[RelatedOccurrence]:
        """Barrel re-export rows of the identifiers."""
        ...

    async def defined_identifiers(
        self,
        occurrences: Sequence[tuple[str, str]],
        scope: SearchScope,
    ) -> dict[tuple[str, str], tuple[str, ...]]:
        """Definition/endpoint names declared inside scoped ``(blob, chunk)`` pairs."""
        ...


def occurrence_excerpt(
    occurrence: RelatedOccurrence, max_lines: int, role: HitRole
) -> SearchHit | None:
    """Cut the lines around an occurrence out of its chunk.

    The excerpt starts at the enclosing definition's header when that header
    is inside the chunk and close enough, so a caller reads as
    ``def create_invoice(...)`` followed by the call, not as a bare call line.
    """
    chunk = occurrence.hit
    lines = chunk.content.splitlines()
    offset = occurrence.line - chunk.start_line
    if offset < 0 or offset >= len(lines) or max_lines <= 0:
        return None
    start = offset
    if occurrence.enclosing:
        header = _header_offset(lines, offset, occurrence.enclosing, max_lines)
        if header is not None:
            start = header
    # The occurrence line must stay inside the excerpt even when the header
    # is far above it; keep the header and let the middle fall out.
    stop = min(len(lines), start + max_lines)
    if offset >= stop:
        start = max(0, offset - max_lines + 1)
        stop = offset + 1
    excerpt = lines[start:stop]
    while excerpt and not excerpt[-1].strip():
        excerpt.pop()
    if not excerpt:
        return None
    return replace(
        chunk,
        content="\n".join(excerpt),
        start_line=chunk.start_line + start,
        end_line=chunk.start_line + start + len(excerpt) - 1,
        score=0.0,
        role=role,
        hop=occurrence.hop,
    )


def _header_offset(
    lines: Sequence[str], offset: int, enclosing: str, max_lines: int
) -> int | None:
    """Nearest line above ``offset`` that declares ``enclosing``, within reach."""
    lowest = max(0, offset - max_lines + 1)
    for index in range(offset, lowest - 1, -1):
        text = lines[index]
        position = text.find(enclosing)
        if position < 0:
            continue
        before = text[position - 1] if position > 0 else " "
        after_index = position + len(enclosing)
        after = text[after_index] if after_index < len(text) else " "
        if (before.isalnum() or before == "_") or (after.isalnum() or after == "_"):
            continue
        # A declaration header names the symbol and opens a body or signature.
        if "(" in text[after_index:] or text.rstrip().endswith((":", "{", "=>")):
            return index
    return None
