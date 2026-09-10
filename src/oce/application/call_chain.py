"""Bounded traversal of indexed call edges; inputs are scoped facts, not pipeline state."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import replace

from loguru import logger

from oce.domain.services.evidence_pack import (
    _handover_window,
    _trim_shown_prefix,
    definition_excerpt,
)
from oce.domain.services.relations import RelatedOccurrence, RelationStore
from oce.domain.services.search import (
    DefinitionHit,
    ExactSearchStore,
    SearchHit,
    SearchScope,
)
from oce.domain.services.symbol_resolution import (
    _IDENTIFIER_NOISE,
    _leaf,
    resolve_qualified_definitions,
)
from oce.shared.config.settings import RetrievalSettings

_CHAIN_MAX_FANOUT = 24


_CHAIN_MAX_EXPANSIONS = 40


_CHAIN_CALLEE_DEPTH = 2


_CHAIN_CALLEE_FANOUT = 6


async def resolve_endpoints(
    store: ExactSearchStore,
    scope: SearchScope,
    identifiers: Sequence[str],
    qualifiers: dict[str, tuple[str, ...]],
) -> list[tuple[str, list[DefinitionHit]]]:
    """Declarations of each queried name, qualified names pinned to their scope.

    Only names that resolve to a bounded number of declarations count as
    endpoints; ``send`` declared in twenty classes anchors nothing unless
    the request qualified it.
    """
    # Qualified spellings are kept for the symbol table; the leaf is what
    # the declaration rows carry.
    leaves = list(dict.fromkeys(_leaf(identifier) for identifier in identifiers))
    if not leaves:
        return []
    # A qualified name may be declared in many types (``route`` on every
    # router); the qualifier picks one afterwards, so the bound is wide
    # for those and tight for bare names.
    qualified = [leaf for leaf in leaves if leaf in qualifiers]
    bare = [leaf for leaf in leaves if leaf not in qualifiers]
    # A qualifier that is the recorded enclosing definition (``Router``
    # for ``Router::route``) pins the leaf in SQL, so ``route`` declared
    # in fifty routers is no obstacle; qualifiers that are only a file or
    # a text mention fall back to the wide lookup resolved below.
    pinned_batches = await asyncio.gather(
        *(
            store.find_definitions(
                identifiers=(leaf,),
                scope=scope,
                max_per_identifier=6,
                enclosing=qualifiers[leaf],
            )
            for leaf in qualified
        )
    )
    pinned = {
        leaf: batch
        for leaf, batch in zip(qualified, pinned_batches, strict=True)
        if batch
    }
    unpinned = [leaf for leaf in qualified if leaf not in pinned]
    batches = await asyncio.gather(
        *(
            store.find_definitions(
                identifiers=group, scope=scope, max_per_identifier=bound
            )
            for group, bound in ((unpinned, 40), (bare, 6))
            if group
        )
    )
    definitions = [
        *(item for batch in pinned.values() for item in batch),
        *(item for batch in batches for item in batch),
    ]
    endpoints: list[tuple[str, list[DefinitionHit]]] = []
    for leaf in leaves:
        found = [item for item in definitions if item.identifier == leaf]
        scopes = qualifiers.get(leaf)
        if scopes and found:
            found = resolve_qualified_definitions(found, {leaf: scopes}, strict=True)
        # Endpoints form a prefix of the names in the question. If the
        # start cannot be resolved, a later target must not become the
        # start of a reversed one-ended trace.
        if not found:
            break
        endpoints.append((leaf, found))
    return endpoints


async def trace_path(
    store: ExactSearchStore,
    scope: SearchScope,
    endpoints: Sequence[tuple[str, list[DefinitionHit]]],
    selected: Sequence[SearchHit],
    settings: RetrievalSettings,
    *,
    max_chars: int | None = None,
) -> list[SearchHit]:
    """Shortest call path from the first named symbol to the second.

    Breadth-first over the indexed call edges: the calls inside a
    declaration's span are resolved to declarations in scope, following
    only names declared in at most two places. Depth, calls examined per
    declaration and declarations expanded are fixed bounds. Every hop is rendered as a definition
    excerpt that reaches the line where the next hop is called.
    """
    target, target_definitions = endpoints[1]
    start = endpoints[0][1]
    # declaration, line of the call to the next hop, enclosing definition
    # of that call (differs from the declaration when the hop is a class
    # whose method makes the call)
    Node = tuple[DefinitionHit, int | None, str]
    queue: list[list[Node]] = [[(definition, None, "")] for definition in start]
    visited: set[tuple[str, int]] = {
        (item.hit.blob_name, item.start_line) for item in start
    }
    expanded = 0
    found: list[Node] | None = None
    while queue and found is None and expanded < _CHAIN_MAX_EXPANSIONS:
        path = queue.pop(0)
        if len(path) > settings.call_chain_max_depth:
            continue
        current, _, _ = path[-1]
        expanded += 1
        try:
            calls = await store.calls_within(
                blob_name=current.hit.blob_name,
                start_line=current.start_line,
                end_line=current.end_line,
                scope=scope,
            )
        except Exception as exc:
            logger.warning("Call lookup failed: {}", type(exc).__name__)
            return []
        calls = [
            (name, line, enclosing)
            for name, line, enclosing in calls
            if name.lower() not in _IDENTIFIER_NOISE
        ][:_CHAIN_MAX_FANOUT]
        if not calls:
            continue
        for name, line, enclosing in calls:
            if name == target:
                chosen = next(
                    (
                        item
                        for item in target_definitions
                        if item.hit.blob_name == current.hit.blob_name
                    ),
                    target_definitions[0],
                )
                found = [*path[:-1], (current, line, enclosing), (chosen, None, "")]
                break
        if found is not None:
            break
        if len(path) >= settings.call_chain_max_depth:
            continue
        names = [name for name, _line, _enclosing in calls]
        try:
            resolved = await store.find_definitions(
                identifiers=names, scope=scope, max_per_identifier=2
            )
        except Exception as exc:
            logger.warning("Definition lookup failed: {}", type(exc).__name__)
            return []
        by_name: dict[str, list[DefinitionHit]] = {}
        for item in resolved:
            by_name.setdefault(item.identifier, []).append(item)
        for name, line, enclosing in calls:
            # A name declared in more than two places is not followed:
            # without types the edge is a guess. Two candidates (an
            # implementation and its shim) are both explored, the one in
            # the caller's own file first; the shortest path wins.
            candidates = by_name.get(name, [])
            candidates.sort(
                key=lambda item: item.hit.blob_name != current.hit.blob_name
            )
            for nxt in candidates:
                key = (nxt.hit.blob_name, nxt.start_line)
                if key in visited:
                    continue
                visited.add(key)
                queue.append([*path[:-1], (current, line, enclosing), (nxt, None, "")])
    if found is None:
        return []
    return await render_chain(
        store, scope, selected, settings, found, max_chars=max_chars
    )


async def trace_callees(
    store: ExactSearchStore,
    scope: SearchScope,
    endpoints: Sequence[tuple[str, list[DefinitionHit]]],
    selected: Sequence[SearchHit],
    settings: RetrievalSettings,
    *,
    max_chars: int | None = None,
) -> list[SearchHit]:
    """Two levels of what the named symbol calls, for a one-ended trace.

    "Trace how ``wsgi_app`` dispatches a request" names where the flow
    starts but not where it ends. The declarations it calls, and what
    those call in turn, are the skeleton of the answer; only names
    declared in at most two places are followed, breadth first, within
    the chain budget.
    """
    start = endpoints[0][1][:1]
    visited: set[tuple[str, int]] = {
        (item.hit.blob_name, item.start_line) for item in start
    }
    frontier: list[DefinitionHit] = list(start)
    hits: list[SearchHit] = []
    used = 0
    budget = settings.call_chain_max_chars
    if max_chars is not None:
        budget = min(budget, max_chars)
    selected_spans = [(hit.blob_name, hit.start_line, hit.end_line) for hit in selected]
    excerpt_lines = max(3, settings.related_snippet_lines // 2)
    for hop in range(1, _CHAIN_CALLEE_DEPTH + 1):
        if not frontier or used >= budget:
            break
        next_frontier: list[DefinitionHit] = []
        for current in frontier[:_CHAIN_CALLEE_FANOUT]:
            try:
                calls = await store.calls_within(
                    blob_name=current.hit.blob_name,
                    start_line=current.start_line,
                    end_line=current.end_line,
                    scope=scope,
                )
            except Exception as exc:
                logger.warning("Call lookup failed: {}", type(exc).__name__)
                return hits
            names = [
                name
                for name, _line, _enclosing in calls
                if name.lower() not in _IDENTIFIER_NOISE
            ][:_CHAIN_MAX_FANOUT]
            if not names:
                continue
            try:
                resolved = await store.find_definitions(
                    identifiers=names, scope=scope, max_per_identifier=2
                )
            except Exception as exc:
                logger.warning("Definition lookup failed: {}", type(exc).__name__)
                return hits
            by_name: dict[str, list[DefinitionHit]] = {}
            for item in resolved:
                by_name.setdefault(item.identifier, []).append(item)
            followed = 0
            for name in names:
                if followed >= _CHAIN_CALLEE_FANOUT:
                    break
                # Names declared in at most two places are followed (an
                # implementation and its shim), the caller's own file first.
                candidates = by_name.get(name, [])
                candidates.sort(
                    key=lambda item: item.hit.blob_name != current.hit.blob_name
                )
                for callee in candidates:
                    key = (callee.hit.blob_name, callee.start_line)
                    if key in visited:
                        continue
                    visited.add(key)
                    followed += 1
                    excerpt = definition_excerpt(callee, excerpt_lines)
                    if excerpt is None or any(
                        blob == excerpt.blob_name
                        and span_start <= excerpt.end_line
                        and span_end >= excerpt.start_line
                        for blob, span_start, span_end in selected_spans
                    ):
                        next_frontier.append(callee)
                        continue
                    if used + len(excerpt.content) > budget:
                        return hits
                    used += len(excerpt.content)
                    hits.append(replace(excerpt, role="chain", hop=hop))
                    next_frontier.append(callee)
        frontier = next_frontier
    return hits


async def render_chain(
    store: ExactSearchStore,
    scope: SearchScope,
    selected: Sequence[SearchHit],
    settings: RetrievalSettings,
    found: Sequence[tuple[DefinitionHit, int | None, str]],
    *,
    max_chars: int | None = None,
) -> list[SearchHit]:
    """Two excerpts per hop when the delegating call sits deep in the body.

    The header names the hop (the declaration the flow passes through);
    the window ending at the call line shows where it hands over to the
    next hop, starting at the enclosing method or closure when that header
    is within reach. A call within the header lines needs one excerpt.
    Headers are placed for every hop before any window spends budget, so
    a long body never truncates the chain to its first hop.
    """
    snippet = settings.related_snippet_lines
    shown = [(hit.blob_name, hit.start_line, hit.end_line) for hit in selected]
    headers: list[SearchHit] = []
    windows: list[SearchHit] = []
    for hop, (definition, call_line, enclosing) in enumerate(found):
        reach = snippet
        if call_line is not None and call_line - definition.start_line < snippet:
            reach = max(reach, call_line - definition.start_line + 1)
        header = definition_excerpt(definition, reach)
        if header is not None:
            headers.append(replace(header, hop=hop))
        if call_line is None or call_line - definition.start_line < snippet:
            continue
        try:
            chunk = await store.chunk_for_line(
                blob_name=definition.hit.blob_name, line=call_line, scope=scope
            )
        except Exception as exc:
            logger.warning("Chunk lookup failed: {}", type(exc).__name__)
            chunk = None
        if chunk is None:
            continue
        window = _handover_window(chunk, call_line, enclosing, snippet)
        if window is not None:
            windows.append(replace(window, hop=hop))
    hits: list[SearchHit] = []
    used = 0
    budget = settings.call_chain_max_chars
    if max_chars is not None:
        budget = min(budget, max_chars)
    for pieces, trim_prefix in ((headers, False), (windows, True)):
        for original in pieces:
            piece = _trim_shown_prefix(original, shown) if trim_prefix else original
            if piece is None or any(
                blob == piece.blob_name
                and start <= piece.end_line
                and end >= piece.start_line
                for blob, start, end in shown
            ):
                continue
            if used + len(piece.content) > budget:
                continue
            used += len(piece.content)
            shown.append((piece.blob_name, piece.start_line, piece.end_line))
            hits.append(replace(piece, role="chain"))
    hits.sort(key=lambda hit: (hit.hop if hit.hop is not None else 0, hit.start_line))
    return hits


async def trace_callers(
    store: RelationStore,
    exact_store: ExactSearchStore | None,
    identifiers: Sequence[str],
    scope: SearchScope,
    *,
    max_hops: int,
    max_callers: int,
) -> list[RelatedOccurrence]:
    direct = await store.find_callers(
        identifiers=identifiers,
        scope=scope,
        limit=max(max_callers * 2, max_callers),
    )
    direct = [replace(item, hop=1) for item in direct]
    by_hop: dict[int, list[RelatedOccurrence]] = {1: direct}
    if max_hops <= 1 or exact_store is None:
        return direct

    seen_names = set(identifiers)
    seen_spans = {
        (item.hit.blob_name, item.hit.start_line, item.hit.end_line, item.enclosing)
        for item in direct
    }
    frontier = tuple(
        dict.fromkeys(
            item.enclosing
            for item in direct
            if item.enclosing and item.enclosing not in seen_names
        )
    )
    for hop in range(2, max_hops + 1):
        if not frontier:
            break
        definitions = await exact_store.find_definitions(
            identifiers=frontier,
            scope=scope,
            max_per_identifier=1,
        )
        resolvable = tuple(dict.fromkeys(item.identifier for item in definitions))
        if not resolvable:
            break
        next_occurrences = await store.find_callers(
            identifiers=resolvable,
            scope=scope,
            limit=max(max_callers * 2, max_callers),
        )
        current: list[RelatedOccurrence] = []
        next_frontier: list[str] = []
        for occurrence in next_occurrences:
            key = (
                occurrence.hit.blob_name,
                occurrence.hit.start_line,
                occurrence.hit.end_line,
                occurrence.enclosing,
            )
            if key in seen_spans:
                continue
            seen_spans.add(key)
            current.append(replace(occurrence, hop=hop))
            if occurrence.enclosing and occurrence.enclosing not in seen_names:
                next_frontier.append(occurrence.enclosing)
        if not current:
            break
        by_hop[hop] = current
        seen_names.update(resolvable)
        frontier = tuple(dict.fromkeys(next_frontier))

    ordered: list[RelatedOccurrence] = []
    for index in range(max_callers * 2):
        for hop in sorted(by_hop):
            values = by_hop[hop]
            if index < len(values):
                ordered.append(values[index])
    return ordered
