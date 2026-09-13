"""Call-chain traces over the indexed call edges.

"How does A reach B" is a bounded breadth-first search from A's declaration
along the calls inside each declaration's span, following only names that
resolve to at most two declarations; a one-ended "trace how A dispatches"
walks two levels of callees instead. Every hop is rendered as a definition
excerpt, plus a window ending at the line that hands over to the next hop
when that call sits deep in the body.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from oce.domain.services.query_classifier import QueryIntent
from oce.domain.services.relations import (
    RelatedOccurrence,
    RelationStore,
    header_offset,
)
from oce.domain.services.retrieval.excerpts import definition_excerpt, spans_overlap
from oce.domain.services.retrieval.names import IDENTIFIER_NOISE
from oce.domain.services.retrieval.state import RetrievalState, lane_failed
from oce.domain.services.search import (
    DefinitionHit,
    ExactSearchStore,
    SearchHit,
    SearchScope,
)
from oce.shared.config.settings import RetrievalSettings

# Two-endpoint search: fixed bounds on the calls examined per declaration
# and the declarations expanded in total.
_CHAIN_MAX_FANOUT = 24
_CHAIN_MAX_EXPANSIONS = 40
# One-ended traces follow what the start calls this deep, this many callees
# per declaration, with half-size excerpts so the second level fits.
_CHAIN_CALLEE_DEPTH = 2
_CHAIN_CALLEE_FANOUT = 6

# declaration, line of the call to the next hop, enclosing definition of that
# call (differs from the declaration when the hop is a class whose method
# makes the call)
_Node = tuple[DefinitionHit, int | None, str]


def handover_window(
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


def trim_shown_prefix(
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


class CallChainTracer:
    def __init__(
        self, store: ExactSearchStore | None, settings: RetrievalSettings
    ) -> None:
        self.store = store
        self.settings = settings

    def traces(self, state: RetrievalState) -> bool:
        return (
            state.intent == QueryIntent.CALL_CHAIN
            and len(state.endpoints) >= 1
            and self.store is not None
            and getattr(self.store, "calls_within", None) is not None
        )

    async def trace(self, state: RetrievalState, *, max_chars: int) -> list[SearchHit]:
        """The chain section: two-ended path, or callees of a lone start."""
        if len(state.endpoints) >= 2:
            return await self.path(state, max_chars=max_chars)
        return await self.callees(state, max_chars=max_chars)

    async def path(
        self, state: RetrievalState, *, max_chars: int | None = None
    ) -> list[SearchHit]:
        """Shortest call path from the first named symbol to the second."""
        store = self.store
        assert store is not None and state.scope is not None
        scope = state.scope
        settings = self.settings
        target, target_definitions = state.endpoints[1]
        start = state.endpoints[0][1]
        queue: list[list[_Node]] = [[(definition, None, "")] for definition in start]
        visited: set[tuple[str, int]] = {
            (item.hit.blob_name, item.start_line) for item in start
        }
        expanded = 0
        found: list[_Node] | None = None
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
                lane_failed(state, "chain", exc)
                return []
            calls = [
                (name, line, enclosing)
                for name, line, enclosing in calls
                if name.lower() not in IDENTIFIER_NOISE
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
                lane_failed(state, "chain", exc)
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
                    queue.append(
                        [*path[:-1], (current, line, enclosing), (nxt, None, "")]
                    )
        if found is None:
            return []
        return await self.render(state, found, max_chars=max_chars)

    async def render(
        self,
        state: RetrievalState,
        found: Sequence[_Node],
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
        store = self.store
        assert store is not None and state.scope is not None
        scope = state.scope
        snippet = self.settings.related_snippet_lines
        chunk_for_line = getattr(store, "chunk_for_line", None)
        shown = [
            (hit.blob_name, hit.start_line, hit.end_line) for hit in state.selected
        ]
        headers: list[SearchHit] = []
        windows: list[SearchHit] = []
        for hop, (definition, call_line, enclosing) in enumerate(found):
            reach = snippet
            if call_line is not None and call_line - definition.start_line < snippet:
                reach = max(reach, call_line - definition.start_line + 1)
            header = definition_excerpt(definition, reach)
            if header is not None:
                headers.append(replace(header, hop=hop))
            if (
                call_line is None
                or call_line - definition.start_line < snippet
                or chunk_for_line is None
            ):
                continue
            try:
                chunk: SearchHit | None = await chunk_for_line(
                    blob_name=definition.hit.blob_name, line=call_line, scope=scope
                )
            except Exception as exc:
                lane_failed(state, "chain", exc)
                chunk = None
            if chunk is None:
                continue
            window = handover_window(chunk, call_line, enclosing, snippet)
            if window is not None:
                windows.append(replace(window, hop=hop))
        hits: list[SearchHit] = []
        used = 0
        budget = self.settings.call_chain_max_chars
        if max_chars is not None:
            budget = min(budget, max_chars)
        for pieces, trim_prefix in ((headers, False), (windows, True)):
            for original in pieces:
                piece = trim_shown_prefix(original, shown) if trim_prefix else original
                if piece is None or spans_overlap(piece, shown):
                    continue
                if used + len(piece.content) > budget:
                    continue
                used += len(piece.content)
                shown.append((piece.blob_name, piece.start_line, piece.end_line))
                hits.append(replace(piece, role="chain"))
        hits.sort(
            key=lambda hit: (hit.hop if hit.hop is not None else 0, hit.start_line)
        )
        return hits

    async def callees(
        self, state: RetrievalState, *, max_chars: int | None = None
    ) -> list[SearchHit]:
        """Two levels of what the named symbol calls, for a one-ended trace.

        "Trace how ``wsgi_app`` dispatches a request" names where the flow
        starts but not where it ends. The declarations it calls, and what
        those call in turn, are the skeleton of the answer; only names
        declared in at most two places are followed, breadth first, within
        the chain budget.
        """
        store = self.store
        assert store is not None and state.scope is not None
        scope = state.scope
        settings = self.settings
        start = state.endpoints[0][1][:1]
        visited: set[tuple[str, int]] = {
            (item.hit.blob_name, item.start_line) for item in start
        }
        frontier: list[DefinitionHit] = list(start)
        hits: list[SearchHit] = []
        used = 0
        budget = settings.call_chain_max_chars
        if max_chars is not None:
            budget = min(budget, max_chars)
        selected_spans = [
            (hit.blob_name, hit.start_line, hit.end_line) for hit in state.selected
        ]
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
                    lane_failed(state, "chain", exc)
                    return hits
                names = [
                    name
                    for name, _line, _enclosing in calls
                    if name.lower() not in IDENTIFIER_NOISE
                ][:_CHAIN_MAX_FANOUT]
                if not names:
                    continue
                try:
                    resolved = await store.find_definitions(
                        identifiers=names, scope=scope, max_per_identifier=2
                    )
                except Exception as exc:
                    lane_failed(state, "chain", exc)
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
                        if excerpt is None or spans_overlap(excerpt, selected_spans):
                            next_frontier.append(callee)
                            continue
                        if used + len(excerpt.content) > budget:
                            return hits
                        used += len(excerpt.content)
                        hits.append(replace(excerpt, role="chain", hop=hop))
                        next_frontier.append(callee)
            frontier = next_frontier
        return hits

    async def callers(
        self,
        identifiers: Sequence[str],
        scope: SearchScope,
        relations: RelationStore,
    ) -> list[RelatedOccurrence]:
        """Callers of the identifiers, then the callers of those, up to the hop cap.

        Each hop is only followed through the unique enclosing definition of
        a caller; the hops are interleaved so the first caller of every hop
        appears before the second caller of any.
        """
        settings = self.settings
        direct = await relations.find_callers(
            identifiers=identifiers,
            scope=scope,
            limit=max(settings.callers_max * 2, settings.callers_max),
        )
        direct = [replace(item, hop=1) for item in direct]
        by_hop: dict[int, list[RelatedOccurrence]] = {1: direct}
        if settings.call_chain_max_hops <= 1 or self.store is None:
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
        for hop in range(2, settings.call_chain_max_hops + 1):
            if not frontier:
                break
            definitions = await self.store.find_definitions(
                identifiers=frontier,
                scope=scope,
                max_per_identifier=1,
            )
            resolvable = tuple(dict.fromkeys(item.identifier for item in definitions))
            if not resolvable:
                break
            next_occurrences = await relations.find_callers(
                identifiers=resolvable,
                scope=scope,
                limit=max(settings.callers_max * 2, settings.callers_max),
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
        for index in range(settings.callers_max * 2):
            for hop in sorted(by_hop):
                values = by_hop[hop]
                if index < len(values):
                    ordered.append(values[index])
        return ordered
