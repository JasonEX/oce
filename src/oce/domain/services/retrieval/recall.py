"""The recall stage: SQL first for structural requests, parallel semantic lanes.

SQL lanes depend only on routing. Symbol/path requests start vectors only
after their structural lookup misses; semantic/reference requests overlap
SQL and model work. Decisive evidence releases any vector waiter while a
sent embedding finishes independently. The stage ends by writing one
``RecallEvidence`` record.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from oce.domain.services.path_search import PathContentStore
from oce.domain.services.query_classifier import QueryIntent
from oce.domain.services.retrieval.plan import release_embedding
from oce.domain.services.retrieval.recall_exact import ExactLane
from oce.domain.services.retrieval.recall_text import LexicalLane, PathLookupLane
from oce.domain.services.retrieval.recall_vector import VectorLanes
from oce.domain.services.retrieval.state import (
    ExactEvidence,
    RecallEvidence,
    RetrievalState,
)
from oce.domain.services.search import SearchHit, search_hit_key
from oce.shared.config.settings import RetrievalSettings

# The SQL lanes in start order: exact, path lookup, lexical, anchors.
SqlLanes = tuple[
    asyncio.Task[ExactEvidence],
    asyncio.Task[dict[str, float]],
    asyncio.Task[tuple[SearchHit, ...]],
    asyncio.Task[tuple[SearchHit, ...]],
]


class Recall:
    def __init__(
        self,
        *,
        settings: RetrievalSettings,
        exact: ExactLane,
        lexical: LexicalLane,
        path_lookup: PathLookupLane,
        vector: VectorLanes,
        path_content_store: PathContentStore | None,
    ) -> None:
        self.settings = settings
        self.exact = exact
        self.lexical = lexical
        self.path_lookup = path_lookup
        self.vector = vector
        self.path_content_store = path_content_store

    def start_sql_lanes(self, state: RetrievalState) -> SqlLanes:
        """Exact, path lookup and routed lexical recall depend only on routing.

        They start before the query embedding round trip. Symbol/path queries
        defer lexical I/O until their structural operator misses; that keeps
        the common exact path fast without giving up the fallback.
        """
        eager_lexical = self.lexical.should_recall_eagerly(state)
        return (
            asyncio.create_task(self.exact.recall(state)),
            asyncio.create_task(self.path_lookup.recall(state)),
            asyncio.create_task(self.lexical.recall(state, routed=eager_lexical)),
            asyncio.create_task(self.exact.recall_anchors(state)),
        )

    def defer_embedding(self, state: RetrievalState) -> bool:
        """Whether an enabled SQL operator can answer without query vectors.

        A miss pays the SQL latency before starting embedding; only symbol
        and explicit-path lookups take that tradeoff. Reference and semantic
        requests retain overlapping SQL/model work.
        """
        if (
            not self.settings.decisive_skips_dense
            or state.scope is None
            or not state.scope.blob_names
        ):
            return False
        if state.route.intent == QueryIntent.SYMBOL:
            return self.exact.available(state)
        return (
            state.route.intent == QueryIntent.PATH
            and self.settings.path_lookup_enabled
            and self.path_lookup.store is not None
            and self.path_content_store is not None
            and state.route.evidence.has_path_evidence
        )

    async def recall(
        self,
        state: RetrievalState,
        sql_lanes: SqlLanes,
        *,
        start_embedding: Callable[[RetrievalState], None],
    ) -> None:
        exact_task, lookup_task, lexical_task, anchor_task = sql_lanes
        vector_lanes = (
            asyncio.create_task(
                self.vector.recall(state, has_fallback=self.has_fallback_recall(state))
            )
            if state.embedding is not None
            else None
        )
        try:
            exact, lookup_scores, lexical, anchors = await asyncio.gather(
                exact_task, lookup_task, lexical_task, anchor_task
            )
        except BaseException:
            if vector_lanes is not None:
                vector_lanes.cancel()
                await asyncio.gather(vector_lanes, return_exceptions=True)
            raise
        if state.audit is not None:
            counts = [count for _name, count in exact.definition_counts]
            state.audit.exact_definitions = sum(counts)
            state.audit.definition_sites = max(counts, default=0)
        evidence = RecallEvidence(
            exact=exact, lexical=lexical, anchors=anchors, lookup_scores=lookup_scores
        )
        reason = self.decisive_reason(state, evidence)
        if reason is not None:
            if vector_lanes is not None:
                vector_lanes.cancel()
                await asyncio.gather(vector_lanes, return_exceptions=True)
            release_embedding(state)
            dense_route = f"skip:{reason}"
            dense: tuple[SearchHit, ...] = ()
            dense_error: Exception | None = None
            path_scores: dict[str, float] = {}
        else:
            if vector_lanes is None:
                start_embedding(state)
                vector_lanes = asyncio.create_task(
                    self.vector.recall(
                        state, has_fallback=self.has_fallback_recall(state)
                    )
                )
            vector = await vector_lanes
            dense, dense_error = tuple(vector.dense), vector.dense_error
            path_scores = vector.path_scores
            dense_route = (
                "dense"
                if dense_error is None
                else f"error:{type(dense_error).__name__}"
            )
            if state.audit is not None:
                for name, elapsed_ms in vector.stages.items():
                    state.audit.record(name, elapsed_ms)
        if state.audit is not None:
            state.audit.dense_route = dense_route
        if not lexical and self.lexical.should_recall_fallback(
            state, exact, lookup_scores
        ):
            lexical = await self.lexical.recall(state, routed=True)
        state.recall = RecallEvidence(
            exact=exact,
            lexical=lexical,
            anchors=anchors,
            lookup_scores=lookup_scores,
            path_scores=path_scores,
            dense=dense,
            dense_error=dense_error,
            dense_route=dense_route,
        )

    def decisive_reason(
        self, state: RetrievalState, evidence: RecallEvidence
    ) -> str | None:
        """Structural evidence that makes vector recall unnecessary, or None.

        Binary facts only: the definition lane found the symbol, the SQL path
        lookup matched a file, or the reference lane found a call/inherit
        site. Import-only evidence does not qualify: the use sites may live in
        code the extractor could not attribute, which lexical and dense recall
        still reach.
        """
        if not self.settings.decisive_skips_dense:
            return None
        intent = state.route.intent
        exact = evidence.exact
        if intent == QueryIntent.SYMBOL and exact.primary_definition_found:
            return "exact_definition"
        if (
            intent == QueryIntent.PATH
            and evidence.lookup_scores
            # The matched file needs a chunk to show; without the content
            # store only dense recall can supply one.
            and self.path_content_store is not None
        ):
            return "path_evidence"
        if intent == QueryIntent.REFERENCE and exact.use_sites:
            # A declaration chunk that also calls the symbol (recursion, a
            # helper next to its use) is not a second place that uses it.
            declared = {search_hit_key(hit) for hit in exact.definitions}
            if any(search_hit_key(hit) not in declared for hit in exact.use_sites):
                return "use_sites"
        return None

    def has_fallback_recall(self, state: RetrievalState) -> bool:
        """Whether another operator can still answer when dense recall fails."""
        return state.scope is not None and (
            self.exact.available(state)
            or self.lexical.can_recall(state)
            or (
                self.path_lookup.store is not None
                and self.path_content_store is not None
                and self.settings.path_lookup_enabled
                and state.route.evidence.has_path_evidence
            )
        )
