"""Assemble the relation sections that follow the primary results.

Every lane hands over occurrences; this module turns them into excerpts with
a role, drops anything the primary results or an earlier section already
show, and spends a per-section cap inside the remaining context budget. The
fill order favours the cheapest, most specific evidence (re-exports, then
definitions, callers, implementations, tests); the display order is fixed by
the formatter and does not depend on which section was filled first.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from oce.domain.services.relations import RelatedOccurrence, occurrence_excerpt
from oce.domain.services.search import HitRole, SearchHit

# Below this many characters an extra section only fragments the answer.
MIN_SECTION_BUDGET = 200


@dataclass(frozen=True)
class SectionInput:
    role: HitRole
    occurrences: Sequence[RelatedOccurrence]
    max_items: int
    max_chars: int


@dataclass(frozen=True)
class EvidencePack:
    hits: list[SearchHit]
    counts: dict[str, int]
    chars: int


def _span_key(hit: SearchHit) -> tuple[str, int, int]:
    return (hit.blob_name, hit.start_line, hit.end_line)


def _overlaps(hit: SearchHit, spans: Sequence[tuple[str, int, int]]) -> bool:
    return any(
        blob == hit.blob_name and start <= hit.end_line and end >= hit.start_line
        for blob, start, end in spans
    )


def assemble_sections(
    *,
    selected: Sequence[SearchHit],
    related: Sequence[SearchHit],
    sections: Sequence[SectionInput],
    remaining_chars: int,
    snippet_lines: int,
) -> EvidencePack:
    """Cut, dedupe and budget the relation sections.

    ``related`` are already-cut definition excerpts from the outbound lane;
    they are counted against the budget first because a definition the
    selected code refers to explains more than a second caller.
    """
    hits: list[SearchHit] = []
    counts: dict[str, int] = {}
    spans = [_span_key(hit) for hit in selected]
    used = 0

    for hit in related:
        if used + len(hit.content) > remaining_chars or _overlaps(hit, spans):
            continue
        hits.append(hit)
        spans.append(_span_key(hit))
        used += len(hit.content)
        counts["related"] = counts.get("related", 0) + 1

    for section in sections:
        budget = min(section.max_chars, remaining_chars - used)
        if budget < MIN_SECTION_BUDGET or not section.occurrences:
            continue
        section_used = 0
        items = 0
        for occurrence in section.occurrences:
            if items >= section.max_items:
                break
            excerpt = occurrence_excerpt(occurrence, snippet_lines, section.role)
            if excerpt is None or _overlaps(excerpt, spans):
                continue
            if section_used + len(excerpt.content) > budget:
                continue
            hits.append(excerpt)
            spans.append(_span_key(excerpt))
            section_used += len(excerpt.content)
            items += 1
        if items:
            counts[section.role] = counts.get(section.role, 0) + items
            used += section_used

    return EvidencePack(hits=hits, counts=counts, chars=used)
