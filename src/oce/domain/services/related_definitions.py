"""Select related declaration excerpts from already fetched, bounded facts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from oce.domain.services.evidence_pack import definition_excerpt
from oce.domain.services.search import (
    DefinitionHit,
    SearchHit,
    SearchHitKey,
    search_hit_key,
)
from oce.domain.services.symbol_resolution import _mine_identifiers
from oce.domain.services.test_paths import is_test_path


@dataclass(frozen=True)
class DefinitionCandidates:
    query_names: tuple[str, ...] = ()
    source_hits: Sequence[SearchHit] = ()
    calls_by_source: Mapping[SearchHitKey, tuple[str, ...]] = field(
        default_factory=dict
    )
    names: tuple[str, ...] = ()
    definitions: Sequence[DefinitionHit] = ()

    def names_for(self, selected: Sequence[SearchHit]) -> tuple[str, ...]:
        """Drop evidence mined only from primary chunks removed by budgeting."""
        kept = {search_hit_key(hit) for hit in selected}
        sources = [hit for hit in self.source_hits if search_hit_key(hit) in kept]
        called = [
            name
            for hit in sources
            for name in self.calls_by_source.get(search_hit_key(hit), ())
        ]
        ordered = dict.fromkeys(
            (*self.query_names, *called, *_mine_identifiers(sources))
        )
        return tuple(name for name in ordered if name in self.names)


def select_related_definitions(
    evidence: DefinitionCandidates,
    selected: Sequence[SearchHit],
    *,
    max_chars: int,
    max_symbols: int,
    snippet_lines: int,
) -> list[SearchHit]:
    candidates = evidence.names_for(selected)
    definitions = evidence.definitions
    selected_keys = {(hit.blob_name, hit.content_hash) for hit in selected}
    selected_spans = [(hit.blob_name, hit.start_line, hit.end_line) for hit in selected]
    by_identifier: dict[str, list[DefinitionHit]] = {}
    for definition in definitions:
        hit = definition.hit
        if (hit.blob_name, hit.content_hash) in selected_keys:
            continue
        if any(
            blob == hit.blob_name and start <= definition.start_line <= end
            for blob, start, end in selected_spans
        ):
            continue
        # A fixture or helper declared in a test file is not the
        # implementation of the name the selected code refers to.
        if is_test_path(hit.path):
            continue
        by_identifier.setdefault(definition.identifier, []).append(definition)
    # A name declared both in the selected code's own file and elsewhere
    # (``request`` as a method and as a module function) resolves to the
    # same-file declaration; the others are a different symbol.
    selected_blobs = {hit.blob_name for hit in selected}
    for identifier, items in by_identifier.items():
        if len(items) > 1:
            local = [item for item in items if item.hit.blob_name in selected_blobs]
            if local:
                by_identifier[identifier] = local

    related: list[SearchHit] = []
    seen: set[tuple[str, int]] = set()
    used_chars = 0
    symbols = 0
    for identifier in candidates:
        if identifier not in by_identifier:
            continue
        if symbols >= max_symbols:
            break
        added = False
        for definition in by_identifier[identifier]:
            key = (definition.hit.blob_name, definition.start_line)
            if key in seen:
                continue
            excerpt = definition_excerpt(definition, snippet_lines)
            if excerpt is None:
                continue
            if used_chars + len(excerpt.content) > max_chars:
                continue
            seen.add(key)
            related.append(excerpt)
            used_chars += len(excerpt.content)
            added = True
        if added:
            symbols += 1
    return related
