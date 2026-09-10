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
from dataclasses import dataclass, replace

from oce.domain.services.relations import (
    RelatedOccurrence,
    header_offset,
    occurrence_excerpt,
)
from oce.domain.services.search import DefinitionHit, HitRole, SearchHit
from oce.domain.services.test_paths import is_test_path

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


def _candidate_value(
    occurrence: RelatedOccurrence,
    excerpt: SearchHit,
    spans: Sequence[tuple[str, int, int]],
    role: HitRole,
) -> tuple[float, int]:
    blobs = {blob for blob, _start, _end in spans}
    new_blob = occurrence.hit.blob_name not in blobs
    new_enclosing = bool(occurrence.enclosing) and not any(
        blob == occurrence.hit.blob_name and start <= occurrence.line <= end
        for blob, start, end in spans
    )
    novelty = 5 if new_blob else 0
    novelty += 2 if new_enclosing else 0
    if role != "test" and is_test_path(occurrence.hit.path):
        novelty -= 3
    specificity = max(0.1, min(1.0, occurrence.hit.score or 0.1))
    cost = max(1, len(excerpt.content))
    return ((novelty + specificity) / (1.0 + cost / 240.0), -cost)


def assemble_sections(
    *,
    selected: Sequence[SearchHit],
    related: Sequence[SearchHit],
    sections: Sequence[SectionInput],
    remaining_chars: int,
    snippet_lines: int,
    related_first: bool = True,
) -> EvidencePack:
    """Cut, dedupe and budget the relation sections.

    ``related`` are already-cut definition excerpts from the outbound lane.
    For a semantic request they are counted against the budget first because
    a definition the selected code refers to explains more than a second
    caller; when the request named the symbol itself, the callers, subtypes,
    tests and re-exports it asked about come first and the mined definitions
    take what is left.
    """
    hits: list[SearchHit] = []
    counts: dict[str, int] = {}
    spans = [_span_key(hit) for hit in selected]
    used = 0

    def fill_related() -> None:
        nonlocal used
        for hit in related:
            if used + len(hit.content) > remaining_chars or _overlaps(hit, spans):
                continue
            hits.append(hit)
            spans.append(_span_key(hit))
            used += len(hit.content)
            counts["related"] = counts.get("related", 0) + 1

    if related_first:
        fill_related()

    for section in sections:
        budget = min(section.max_chars, remaining_chars - used)
        if budget < MIN_SECTION_BUDGET or not section.occurrences:
            continue
        section_used = 0
        items = 0
        pending = list(enumerate(section.occurrences))
        while pending and items < section.max_items:
            candidates: list[tuple[tuple[float, int], int, SearchHit]] = []
            for index, occurrence in pending:
                excerpt = occurrence_excerpt(occurrence, snippet_lines, section.role)
                if excerpt is None or _overlaps(excerpt, spans):
                    continue
                if section_used + len(excerpt.content) > budget:
                    continue
                candidates.append(
                    (
                        _candidate_value(occurrence, excerpt, spans, section.role),
                        -index,
                        excerpt,
                    )
                )
            if not candidates:
                break
            _value, selected_index, excerpt = max(candidates)
            pending = [item for item in pending if item[0] != -selected_index]
            hits.append(excerpt)
            spans.append(_span_key(excerpt))
            section_used += len(excerpt.content)
            items += 1
        if items:
            counts[section.role] = counts.get(section.role, 0) + items
            used += section_used

    if not related_first:
        fill_related()

    return EvidencePack(hits=hits, counts=counts, chars=used)


def _handover_window(
    chunk: SearchHit, line: int, enclosing: str, max_lines: int
) -> SearchHit | None:
    """The lines leading up to ``line`` inside ``chunk``, ending at that line.

    The window opens at the enclosing method or closure header when that is
    within reach, so a handover reads as ``def next(err):`` followed by the
    call, and otherwise shows the ``max_lines`` lines that precede the call.
    """
    lines = chunk.content.splitlines()
    offset = line - chunk.start_line
    if offset < 0 or offset >= len(lines) or max_lines <= 0:
        return None
    start = max(0, offset - max_lines + 1)
    if enclosing:
        header = header_offset(lines, offset, enclosing, max_lines)
        if header is not None:
            start = header
    return replace(
        chunk,
        content="\n".join(lines[start : offset + 1]),
        start_line=chunk.start_line + start,
        end_line=line,
        score=0.0,
        role="chain",
    )


def _trim_shown_prefix(
    hit: SearchHit, shown: Sequence[tuple[str, int, int]]
) -> SearchHit | None:
    """Remove an already-shown prefix while keeping a later hand-over line."""
    covered_through = max(
        (
            end
            for blob, start, end in shown
            if blob == hit.blob_name and start <= hit.start_line <= end
        ),
        default=hit.start_line - 1,
    )
    if covered_through < hit.start_line:
        return hit
    if covered_through >= hit.end_line:
        return None
    offset = covered_through - hit.start_line + 1
    return replace(
        hit,
        content="\n".join(hit.content.splitlines()[offset:]),
        start_line=covered_through + 1,
    )


def definition_excerpt(definition: DefinitionHit, max_lines: int) -> SearchHit | None:
    """Cut the first ``max_lines`` lines of a definition out of its chunk."""
    chunk = definition.hit
    lines = chunk.content.splitlines()
    offset = definition.start_line - chunk.start_line
    if offset < 0 or offset >= len(lines):
        return None
    span = min(definition.end_line - definition.start_line + 1, max_lines)
    excerpt = lines[offset : offset + span]
    while excerpt and not excerpt[-1].strip():
        excerpt.pop()
    if not excerpt:
        return None
    return replace(
        chunk,
        content="\n".join(excerpt),
        start_line=definition.start_line,
        end_line=definition.start_line + len(excerpt) - 1,
        score=0.0,
        role="related",
    )


def merge_adjacent_hits(hits: Sequence[SearchHit]) -> list[SearchHit]:
    """Join hits of one file whose line spans touch or overlap.

    The merged hit keeps the rank position of its best member and stitches
    text by line number, so the result renders exactly like the source.
    """
    groups: dict[tuple[str, str], list[int]] = {}
    for index, hit in enumerate(hits):
        groups.setdefault((hit.blob_name, hit.path), []).append(index)

    merged_at: dict[int, SearchHit] = {}
    consumed: set[int] = set()
    for indices in groups.values():
        ordered = sorted(indices, key=lambda index: hits[index].start_line)
        cluster: list[int] = []
        cluster_end = 0
        for index in ordered:
            hit = hits[index]
            if cluster and hit.start_line <= cluster_end + 1:
                cluster.append(index)
                cluster_end = max(cluster_end, hit.end_line)
                continue
            if cluster:
                _emit_cluster(hits, cluster, merged_at, consumed)
            cluster = [index]
            cluster_end = hit.end_line
        if cluster:
            _emit_cluster(hits, cluster, merged_at, consumed)

    return [
        merged_at[index]
        for index in range(len(hits))
        if index in merged_at and index not in consumed
    ]


def _emit_cluster(
    hits: Sequence[SearchHit],
    cluster: list[int],
    merged_at: dict[int, SearchHit],
    consumed: set[int],
) -> None:
    anchor = min(cluster)
    if len(cluster) == 1:
        merged_at[anchor] = hits[anchor]
        return
    lines: dict[int, str] = {}
    end_line = 0
    for index in cluster:
        hit = hits[index]
        for offset, text in enumerate(hit.content.splitlines()):
            lines.setdefault(hit.start_line + offset, text)
        end_line = max(end_line, hit.end_line)
    start_line = hits[cluster[0]].start_line
    content = "\n".join(
        lines.get(number, "") for number in range(start_line, end_line + 1)
    )
    best = max((hits[index] for index in cluster), key=lambda hit: hit.score)
    contexts = {hits[index].context for index in cluster}
    merged_at[anchor] = replace(
        hits[cluster[0]],
        content=content,
        start_line=start_line,
        end_line=end_line,
        score=best.score,
        content_hash="",
        context=contexts.pop() if len(contexts) == 1 else None,
    )
    consumed.update(index for index in cluster if index != anchor)
