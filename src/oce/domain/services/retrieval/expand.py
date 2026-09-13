"""The expand stage: adjacent merges and the relation sections.

After selection the primary hits are what answers the request; this stage
appends what an editing task asks next. Each relation is one bounded SQL
lookup rendered as signature-sized excerpts with its own slot and character
cap: the definitions the selected code refers to, its callers, its
implementations, the tests that exercise it, the barrel files that re-export
it, and for call-chain requests the traced path itself.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from oce.domain.services.evidence_pack import SectionInput, assemble_sections
from oce.domain.services.query_classifier import QueryIntent
from oce.domain.services.relations import RelatedOccurrence, RelationStore
from oce.domain.services.retrieval.budgets import (
    MIN_RELATED_BUDGET,
    context_budget,
    selected_chars,
)
from oce.domain.services.retrieval.chain import CallChainTracer
from oce.domain.services.retrieval.excerpts import (
    definition_excerpt,
    merge_adjacent_hits,
)
from oce.domain.services.retrieval.names import (
    IDENTIFIER_NOISE,
    resolve_qualified_hits,
)
from oce.domain.services.retrieval.state import RetrievalState, lane_failed
from oce.domain.services.search import (
    DefinitionHit,
    ExactSearchStore,
    HitRole,
    SearchHit,
    search_hit_key,
)
from oce.domain.services.test_paths import is_test_path
from oce.shared.config.settings import RetrievalSettings

# Identifiers worth pulling a definition for: multi-part or reasonably long
# names. Short lowercase words are mostly keywords, locals, or English.
_MINED_IDENTIFIER = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b")


def mine_identifiers(hits: Sequence[SearchHit]) -> list[str]:
    """Identifiers referenced by the hits, most widely shared first.

    cAST may place an enclosing class or function signature in ``context``
    rather than repeating it in a split method chunk. Treat that structural
    header as part of the hit for relation expansion, otherwise base classes
    disappear precisely when chunking is most accurate.
    """
    per_hit: dict[str, set[int]] = {}
    total: dict[str, int] = {}
    for index, hit in enumerate(hits):
        for text in (hit.content, hit.context or ""):
            for match in _MINED_IDENTIFIER.finditer(text):
                token = match.group()
                lowered = token.lower()
                if lowered in IDENTIFIER_NOISE:
                    continue
                # A plain lowercase word needs some length to look like a symbol.
                if token.islower() and "_" not in token and len(token) < 6:
                    continue
                per_hit.setdefault(token, set()).add(index)
                total[token] = total.get(token, 0) + 1
    return sorted(
        per_hit,
        key=lambda token: (-len(per_hit[token]), -total[token], token),
    )


@dataclass(frozen=True)
class RelationLane:
    role: HitRole
    fetch: Callable[[Sequence[str]], Awaitable[list[RelatedOccurrence]]]
    max_items: int
    max_chars: int


async def _no_hits() -> list[SearchHit]:
    return []


async def _no_occurrences() -> list[RelatedOccurrence]:
    return []


class Expander:
    def __init__(
        self,
        *,
        settings: RetrievalSettings,
        exact_store: ExactSearchStore | None,
        relation_store: RelationStore | None,
        chain: CallChainTracer,
    ) -> None:
        self.settings = settings
        self.exact_store = exact_store
        self.relation_store = relation_store
        self.chain = chain

    def expands_relations(self, state: RetrievalState) -> bool:
        if state.scope is None or not state.scope.blob_names:
            return False
        return (
            self.wants_related(state)
            or bool(self.relation_lanes(state))
            or self.chain.traces(state)
        )

    def wants_related(self, state: RetrievalState) -> bool:
        return (
            self.settings.related_definitions_enabled
            and state.strategy.expand_related_definitions
            and self.exact_store is not None
        )

    async def expand(self, state: RetrievalState) -> None:
        settings = self.settings
        if settings.merge_adjacent_enabled:
            state.selected = merge_adjacent_hits(state.selected)
        if not state.selected or not self.expands_relations(state):
            return
        assert state.scope is not None
        with state.stage("expand"):
            lanes = self.relation_lanes(state)
            relation_cap = self.relation_budget_cap(state, lanes)
            budget = context_budget(settings, state)
            remaining = budget - selected_chars(state)
            chain: list[SearchHit] = []
            if self.chain.traces(state):
                # A chain may displace the primary tail, but the leading
                # answer remains. Bound the chain before rendering so even an
                # operator-supplied chain cap larger than the context cannot
                # break the hard response budget.
                chain_budget = max(0, budget - len(state.selected[0].content))
                if chain_budget > 0:
                    chain = await self.chain.trace(state, max_chars=chain_budget)
                if chain:
                    self.make_relation_room(
                        state, sum(len(hit.content) for hit in chain)
                    )
                    remaining = (
                        budget
                        - selected_chars(state)
                        - sum(len(hit.content) for hit in chain)
                    )
            identifiers = state.lookup_identifiers
            if lanes and not identifiers:
                # A feature request names no symbol; the symbols its top
                # results declare are what its tests exercise.
                identifiers = await self.selected_definition_names(state)
            wants_related = self.wants_related(state)
            related_result, lane_results = await asyncio.gather(
                (
                    self.related_definitions(state, budget=max(remaining, relation_cap))
                    if wants_related
                    else _no_hits()
                ),
                asyncio.gather(
                    *(
                        lane.fetch(identifiers) if identifiers else _no_occurrences()
                        for lane in lanes
                    ),
                    return_exceptions=True,
                ),
                return_exceptions=True,
            )
            related: list[SearchHit] = []
            if isinstance(related_result, BaseException):
                lane_failed(state, "related", related_result)
            else:
                related = related_result
            sections: list[SectionInput] = []
            # The inner gather never raises with ``return_exceptions``; the
            # outer one only reports its own cancellation.
            if isinstance(lane_results, BaseException):
                raise lane_results
            for lane, result in zip(lanes, lane_results, strict=True):
                if isinstance(result, BaseException):
                    lane_failed(state, lane.role, result)
                    continue
                sections.append(
                    SectionInput(lane.role, result, lane.max_items, lane.max_chars)
                )
            related_first = state.intent not in (
                QueryIntent.SYMBOL,
                QueryIntent.REFERENCE,
            )
            shown = [*state.selected, *chain]
            preview = assemble_sections(
                selected=shown,
                related=related,
                sections=sections,
                remaining_chars=relation_cap if relation_cap > 0 else max(remaining, 0),
                snippet_lines=settings.relation_snippet_lines,
                related_first=related_first,
            )
            if preview.hits and preview.chars > remaining:
                chain_chars = sum(len(hit.content) for hit in chain)
                self.make_relation_room(
                    state, chain_chars + min(preview.chars, relation_cap)
                )
                remaining = budget - selected_chars(state) - chain_chars
                shown = [*state.selected, *chain]
                if wants_related:
                    try:
                        related = await self.related_definitions(
                            state, budget=min(remaining, relation_cap)
                        )
                    except Exception as exc:
                        # The preview remains valid when its refresh fails.
                        lane_failed(state, "related", exc)
            # A tiny tail budget produces fragmented signatures that cost
            # another SQL lookup without explaining a relationship.
            if remaining < MIN_RELATED_BUDGET:
                state.related = chain
                if state.audit is not None and chain:
                    state.audit.relation_counts = {"chain": len(chain)}
                    state.audit.relation_chars = sum(len(hit.content) for hit in chain)
                return
            pack = assemble_sections(
                selected=shown,
                related=related,
                sections=sections,
                remaining_chars=(
                    min(remaining, relation_cap) if relation_cap > 0 else remaining
                ),
                snippet_lines=settings.relation_snippet_lines,
                related_first=related_first,
            )
            state.related = [*chain, *pack.hits]
            if state.audit is not None:
                counts = dict(pack.counts)
                if chain:
                    counts["chain"] = len(chain)
                state.audit.relation_counts = counts
                state.audit.relation_chars = pack.chars + sum(
                    len(hit.content) for hit in chain
                )

    def relation_budget_cap(
        self, state: RetrievalState, lanes: Sequence[RelationLane]
    ) -> int:
        """Bound relation spend by active lane caps and the context scale.

        The configured value remains an upper bound, not an unconditional
        reservation. A quarter of the active context keeps answers readable
        across focused and broad selection budgets without tying the policy to
        one benchmark's chunk sizes.
        """
        active = sum(lane.max_chars for lane in lanes)
        if state.strategy.expand_related_definitions and self.exact_store is not None:
            active += self.settings.related_max_chars
        if active <= 0:
            return 0
        context = context_budget(self.settings, state)
        return min(
            self.settings.relation_reserve_chars,
            active,
            max(MIN_RELATED_BUDGET, context // 4),
        )

    def make_relation_room(self, state: RetrievalState, target: int) -> None:
        """Trim only the lowest-priority primary tail when evidence exists."""
        if target <= 0:
            return
        budget = context_budget(self.settings, state)
        kept = list(state.selected)
        while kept and sum(len(hit.content) for hit in kept) + target > budget:
            kept.pop()
        if kept:
            state.selected = kept

    def relation_lanes(self, state: RetrievalState) -> list[RelationLane]:
        """Relation sections the intent asks for, in fill order.

        Re-exports are tiny and pin the public import path, so they are filled
        first; tests come last because a test file is the largest excerpt and
        the least specific to the exact question.
        """
        store = self.relation_store
        if store is None or state.scope is None or not state.scope.blob_names:
            return []
        scope = state.scope
        settings = self.settings
        strategy = state.strategy
        lanes: list[RelationLane] = []
        if strategy.expand_reexports and settings.reexports_enabled:
            lanes.append(
                RelationLane(
                    "reexport",
                    lambda names: store.find_reexports(
                        identifiers=names, scope=scope, limit=settings.reexports_max
                    ),
                    settings.reexports_max,
                    settings.reexports_max_chars,
                )
            )
        if strategy.expand_callers and settings.callers_enabled:
            if state.intent == QueryIntent.CALL_CHAIN:

                async def fetch_callers(
                    names: Sequence[str],
                ) -> list[RelatedOccurrence]:
                    return await self.chain.callers(names, scope, store)
            else:

                async def fetch_callers(
                    names: Sequence[str],
                ) -> list[RelatedOccurrence]:
                    return await store.find_callers(
                        identifiers=names,
                        scope=scope,
                        limit=settings.callers_max * 2,
                    )

            lanes.append(
                RelationLane(
                    "caller",
                    fetch_callers,
                    settings.callers_max,
                    settings.callers_max_chars,
                )
            )
        if strategy.expand_implementations and settings.implementations_enabled:
            lanes.append(
                RelationLane(
                    "implementation",
                    lambda names: store.find_implementations(
                        identifiers=names,
                        scope=scope,
                        limit=settings.implementations_max * 2,
                    ),
                    settings.implementations_max,
                    settings.implementations_max_chars,
                )
            )
        if strategy.expand_tests and settings.tests_enabled:
            # "Where is X defined" wants the declaration; one test shows how
            # it is exercised. Requests that ask for tests keep the full slot.
            tests_max = 1 if state.intent == QueryIntent.SYMBOL else settings.tests_max
            lanes.append(
                RelationLane(
                    "test",
                    lambda names: store.find_test_uses(
                        identifiers=names, scope=scope, limit=tests_max * 2
                    ),
                    tests_max,
                    settings.tests_max_chars,
                )
            )
        return lanes

    async def selected_definition_names(self, state: RetrievalState) -> tuple[str, ...]:
        store = self.relation_store
        if store is None or state.scope is None:
            return ()
        sources = state.selected[: self.settings.related_source_hits]
        pairs = [
            (hit.blob_name, hit.content_hash)
            for hit in sources
            if hit.blob_name and hit.content_hash
        ]
        if not pairs:
            return ()
        try:
            defined = await store.defined_identifiers(pairs, state.scope)
        except Exception as exc:
            lane_failed(state, "defined_names", exc)
            return ()
        names: list[str] = []
        for pair in pairs:
            for name in defined.get(pair, ()):
                if name not in names:
                    names.append(name)
        return tuple(names[: self.settings.related_max_symbols])

    async def declarations_of(
        self, state: RetrievalState, identifiers: Sequence[str]
    ) -> list[DefinitionHit]:
        """The resolved declarations of a reference request's identifiers."""
        assert self.exact_store is not None and state.scope is not None
        rows = await self.exact_store.find_definitions(
            identifiers=identifiers, scope=state.scope, max_per_identifier=40
        )
        declared = {search_hit_key(hit) for hit in state.definitions}
        return [item for item in rows if search_hit_key(item.hit) in declared]

    async def related_definitions(
        self, state: RetrievalState, *, budget: int | None = None
    ) -> list[SearchHit]:
        """Signature-sized excerpts of symbols the selected code refers to.

        Query identifiers come first: when the request names a symbol whose
        definition the selection missed, that is the most useful pull. Mined
        identifiers follow, ordered by how many selected hits share them.
        """
        settings = self.settings
        assert self.exact_store is not None and state.scope is not None
        remaining_chars = context_budget(settings, state) - selected_chars(state)
        # A tiny tail budget produces fragmented signatures that cost another
        # SQL lookup without explaining a relationship. Keep expansion useful
        # and predictable instead of filling every last character.
        available = max(remaining_chars, budget or 0)
        if available < MIN_RELATED_BUDGET:
            return []
        related_budget = min(settings.related_max_chars, available)
        sources = state.selected[: settings.related_source_hits]
        # Names the selected code calls are a stronger relation than names it
        # merely mentions (types in annotations, words in docstrings), so
        # they are pulled first.
        called: list[str] = []
        calls_within = getattr(self.exact_store, "calls_within", None)
        if calls_within is not None and state.intent != QueryIntent.REFERENCE:
            try:
                call_lists: list[list[tuple[str, int, str]]] = await asyncio.gather(
                    *(
                        calls_within(
                            blob_name=hit.blob_name,
                            start_line=hit.start_line,
                            end_line=hit.end_line,
                            scope=state.scope,
                        )
                        for hit in sources
                        if hit.blob_name
                    )
                )
            except Exception as exc:
                lane_failed(state, "related", exc)
                call_lists = []
            for calls in call_lists:
                for name, _line, _enclosing in calls:
                    if name.lower() not in IDENTIFIER_NOISE and name not in called:
                        called.append(name)
        ordered: list[str] = []
        # "Where is X used" asks about X: the excerpt worth appending is X's
        # own declaration when the use sites crowded it out, not the
        # definitions of whatever else those use sites happen to call.
        mined: tuple[str, ...] = (
            ()
            if state.intent == QueryIntent.REFERENCE
            else (*called, *mine_identifiers(sources))
        )
        for identifier in (*state.lookup_identifiers, *mined):
            if identifier not in ordered:
                ordered.append(identifier)
        # Symbols defined by the selected code itself need no pull-in; the
        # store returns their chunk so the filter below drops them.
        candidates = ordered[: settings.related_max_symbols * 5]
        if not candidates:
            return []

        if state.intent == QueryIntent.REFERENCE and state.definitions:
            # The exact lane already resolved the declaration, qualifier
            # included; ``render`` declared in five files would otherwise
            # exceed the ambiguity bound and the answer's own declaration
            # would never be appended.
            definitions = await self.declarations_of(state, candidates)
        else:
            definitions = await self.exact_store.find_definitions(
                identifiers=candidates,
                scope=state.scope,
                max_per_identifier=settings.related_max_definitions_per_symbol,
            )
        selected_keys = {(hit.blob_name, hit.content_hash) for hit in state.selected}
        selected_spans = [
            (hit.blob_name, hit.start_line, hit.end_line) for hit in state.selected
        ]
        # A qualified name pins its leaf to a scope for the related pull exactly
        # as it does for the primary lane: "where is ``Flask.make_response``
        # defined" must not append ``helpers.make_response``, a different
        # function that happens to share the leaf, as the first thing after
        # the answer. The chunk evidence decides here, not the recorded
        # enclosing name: the exact lane pinned the primary answer the same way.
        for leaf_name, scopes in state.qualifiers.items():
            pinned = [item for item in definitions if item.identifier == leaf_name]
            if not pinned:
                continue
            kept = {
                search_hit_key(hit)
                for hit in resolve_qualified_hits(
                    [item.hit for item in pinned], {leaf_name: scopes}
                )
            }
            definitions = [
                item
                for item in definitions
                if item.identifier != leaf_name or search_hit_key(item.hit) in kept
            ]
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
        selected_blobs = {hit.blob_name for hit in state.selected}
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
            if symbols >= settings.related_max_symbols:
                break
            added = False
            for definition in by_identifier[identifier]:
                key = (definition.hit.blob_name, definition.start_line)
                if key in seen:
                    continue
                excerpt = definition_excerpt(definition, settings.related_snippet_lines)
                if excerpt is None:
                    continue
                if used_chars + len(excerpt.content) > related_budget:
                    continue
                seen.add(key)
                related.append(excerpt)
                used_chars += len(excerpt.content)
                added = True
            if added:
                symbols += 1
        return related
