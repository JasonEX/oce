"""The recall stage: every lane in parallel, the vector lanes only when needed.

The SQL lanes depend only on routing, so they start before the embedding
round trip and usually finish first. A definition, a matched path or a call
site is the whole answer to a symbol, path or reference request, and every
one of them comes from SQL; the vector lanes are dropped the moment the SQL
lanes prove the request decisive and awaited exactly as before otherwise.
"""

from __future__ import annotations

import asyncio

from oce.domain.services.path_search import PathContentStore
from oce.domain.services.query_classifier import QueryIntent
from oce.domain.services.retrieval.hubs import recall_hubs
from oce.domain.services.retrieval.plan import release_embedding
from oce.domain.services.retrieval.recall_exact import ExactLane
from oce.domain.services.retrieval.recall_text import LexicalLane, PathLookupLane
from oce.domain.services.retrieval.recall_vector import VectorLanes
from oce.domain.services.retrieval.state import RetrievalState
from oce.domain.services.search import HubDefinition, SearchHit, search_hit_key
from oce.shared.config.settings import RetrievalSettings

# The SQL lanes in start order: exact, path lookup, lexical, anchors, hubs.
SqlLanes = tuple[
    asyncio.Task[tuple[list[SearchHit], list[SearchHit], list[SearchHit]]],
    asyncio.Task[dict[str, float]],
    asyncio.Task[list[SearchHit]],
    asyncio.Task[list[SearchHit]],
    asyncio.Task[list[HubDefinition]],
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
            asyncio.create_task(recall_hubs(state, self.settings, self.exact.store)),
        )

    async def recall(self, state: RetrievalState, sql_lanes: SqlLanes) -> None:
        exact_task, lookup_task, lexical_task, anchor_task, hub_task = sql_lanes
        vector_lanes = asyncio.create_task(self.vector.recall(state))
        try:
            (
                (state.exact, state.definitions, state.use_sites),
                state.lookup_scores,
                state.lexical,
                state.anchors,
                state.hubs,
            ) = await asyncio.gather(
                exact_task, lookup_task, lexical_task, anchor_task, hub_task
            )
        except BaseException:
            vector_lanes.cancel()
            await asyncio.gather(vector_lanes, return_exceptions=True)
            raise
        reason = self.decisive_reason(state)
        if reason is not None:
            vector_lanes.cancel()
            await asyncio.gather(vector_lanes, return_exceptions=True)
            release_embedding(state)
            state.dense_route = f"skip:{reason}"
        else:
            (state.dense, state.dense_error), state.path_scores = await vector_lanes
            state.dense_route = (
                "dense"
                if state.dense_error is None
                else f"error:{type(state.dense_error).__name__}"
            )
            if state.audit is not None:
                for name, elapsed_ms in state.vector_stages.items():
                    state.audit.record(name, elapsed_ms)
        if state.audit is not None:
            state.audit.dense_route = state.dense_route
        if not state.lexical and self.lexical.should_recall_fallback(state):
            state.lexical = await self.lexical.recall(state, routed=True)

    def decisive_reason(self, state: RetrievalState) -> str | None:
        """Structural evidence that makes vector recall unnecessary, or None.

        Binary facts only: the definition lane found the symbol, the SQL path
        lookup matched a file, or the reference lane found a call/inherit
        site. Import-only evidence does not qualify: the use sites may live in
        code the extractor could not attribute, which lexical and dense recall
        still reach.
        """
        if not self.settings.decisive_skips_dense or state.embed_task is None:
            return None
        if state.intent == QueryIntent.SYMBOL and state.primary_definition_found:
            return "exact_definition"
        if (
            state.intent == QueryIntent.PATH
            and state.lookup_scores
            # The matched file needs a chunk to show; without the content
            # store only dense recall can supply one.
            and self.path_content_store is not None
        ):
            return "path_evidence"
        if state.intent == QueryIntent.REFERENCE and state.use_sites:
            # A declaration chunk that also calls the symbol (recursion, a
            # helper next to its use) is not a second place that uses it.
            declared = {search_hit_key(hit) for hit in state.definitions}
            if any(search_hit_key(hit) not in declared for hit in state.use_sites):
                return "use_sites"
        return None

    def has_fallback_recall(self, state: RetrievalState) -> bool:
        """Whether another operator can still answer when dense recall fails."""
        evidence = state.evidence
        return (
            evidence is not None
            and state.scope is not None
            and (
                self.exact.available(state)
                or self.lexical.can_recall(state)
                or (
                    self.path_lookup.store is not None
                    and self.path_content_store is not None
                    and self.settings.path_lookup_enabled
                    and evidence.has_path_evidence
                )
            )
        )
