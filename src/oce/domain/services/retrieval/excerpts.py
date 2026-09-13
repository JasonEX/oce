"""Cutting and stitching hit text: definition excerpts and adjacent merges."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from oce.domain.services.search import DefinitionHit, SearchHit


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


def spans_overlap(hit: SearchHit, spans: Sequence[tuple[str, int, int]]) -> bool:
    """Whether ``hit`` shares any line with one of the ``(blob, start, end)`` spans."""
    return any(
        blob == hit.blob_name and start <= hit.end_line and end >= hit.start_line
        for blob, start, end in spans
    )
