"""``RetrievalPipeline``: the fixed sequence of stages one request goes through.

    route   query -> intent / strategy / QueryEvidence
    plan    optional LLM rewrite + facet decomposition + query vectors (started)
    recall  dense | exact | intent-routed lexical | path lanes, in parallel
    fuse    RRF over dense/lexical -> exact merge -> path boost/backfill
    prior   source and working-set priors -> bounded structural heads -> floor
    rerank  plan_rerank decision -> dedicated reranker -> chat-LLM reranker
    select  focused / coverage selection under a hard character budget
    expand  adjacent merge -> relation sections within the remaining budget

Rerank solves "several relevant chunks in one file", select solves "this set
is complete without redundancy", expand solves "what the returned chunks refer
to". With its switch off every stage is the identity transform. Each stage
lives in its own module and owns the fields of ``RetrievalState`` it fills;
this class wires them and holds the cancellation discipline of ``search``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING

from loguru import logger

from oce.domain.services.embedder import Embedder
from oce.domain.services.path_search import PathContentStore, PathSearchStore
from oce.domain.services.query_classifier import (
    classify_query_intent,
    should_use_path_index,
)
from oce.domain.services.query_evidence import extract_query_evidence
from oce.domain.services.query_planner import HeuristicQueryPlanner, QueryPlanner
from oce.domain.services.relations import RelationStore
from oce.domain.services.reranker import Reranker
from oce.domain.services.retrieval.budgets import context_budget
from oce.domain.services.retrieval.chain import CallChainTracer
from oce.domain.services.retrieval.expand import Expander
from oce.domain.services.retrieval.fuse import Fusion
from oce.domain.services.retrieval.names import split_qualified_identifiers
from oce.domain.services.retrieval.plan import QueryPlan, release_embedding
from oce.domain.services.retrieval.priors import (
    neutral_priority_factor,
    source_priority_factor,
)
from oce.domain.services.retrieval.rank import Ranker
from oce.domain.services.retrieval.recall import Recall
from oce.domain.services.retrieval.recall_exact import ExactLane
from oce.domain.services.retrieval.recall_text import LexicalLane, PathLookupLane
from oce.domain.services.retrieval.recall_vector import VectorLanes
from oce.domain.services.retrieval.state import RetrievalState
from oce.domain.services.retrieval_strategy import get_strategy
from oce.domain.services.search import (
    ExactSearchStore,
    LexicalSearchStore,
    PathLookupStore,
    SearchHit,
    SearchScope,
    SearchStore,
)
from oce.domain.services.selector.coverage_selector import CoverageSelector
from oce.domain.services.selector.protocols import Selector
from oce.domain.services.selector.topk_selector import TopKSelector
from oce.shared.config.settings import RetrievalSettings
from oce.shared.metrics import RetrievalAudit

if TYPE_CHECKING:
    from oce.domain.services.llm.rewriter import QueryRewriter


class RetrievalPipeline:
    def __init__(
        self,
        *,
        embedder: Embedder,
        store: SearchStore,
        settings: RetrievalSettings,
        reranker: Reranker | None = None,
        llm_reranker: Reranker | None = None,
        query_rewriter: QueryRewriter | None = None,
        path_store: PathSearchStore | None = None,
        path_content_store: PathContentStore | None = None,
        path_lookup_store: PathLookupStore | None = None,
        exact_store: ExactSearchStore | None = None,
        relation_store: RelationStore | None = None,
        lexical_store: LexicalSearchStore | None = None,
        selector: Selector | None = None,
        query_planner: QueryPlanner | None = None,
        priority_factor: Callable[[str], float] | None = None,
        rerank_window: int | None = None,
    ) -> None:
        self.settings = settings
        self.path_store = path_store
        self.priority_factor = priority_factor or (
            source_priority_factor
            if settings.source_priority_enabled
            else neutral_priority_factor
        )
        planner = query_planner or HeuristicQueryPlanner(
            max_queries=(
                settings.query_max_queries
                if settings.query_decomposition_enabled
                else 1
            ),
            min_facet_chars=settings.query_min_facet_chars,
        )
        if selector is not None:
            self.selector = selector
        elif settings.coverage_selection_enabled:
            self.selector = CoverageSelector(
                max_per_path=settings.max_chunks_per_path,
                focused_max_per_path=settings.focused_max_chunks_per_path,
                max_chars=settings.max_context_chars,
                focused_max_chars=settings.focused_max_context_chars,
                overlap_threshold=settings.overlap_threshold,
            )
        else:
            self.selector = TopKSelector()

        self.plan = QueryPlan(
            embedder=embedder, query_planner=planner, query_rewriter=query_rewriter
        )
        self.exact = ExactLane(exact_store, settings)
        self.recall = Recall(
            settings=settings,
            exact=self.exact,
            lexical=LexicalLane(lexical_store, settings),
            path_lookup=PathLookupLane(path_lookup_store, settings),
            vector=VectorLanes(
                store=store,
                path_store=path_store,
                settings=settings,
                has_fallback=self._has_fallback_recall,
            ),
            path_content_store=path_content_store,
        )
        self.fusion = Fusion(
            settings=settings,
            path_content_store=path_content_store,
            priority_factor=self.priority_factor,
            rerank_window=rerank_window,
        )
        self.ranker = Ranker(
            settings=settings,
            priority_factor=self.priority_factor,
            exact_store=exact_store,
            relation_store=relation_store,
            reranker=reranker,
            llm_reranker=llm_reranker,
        )
        self.chain = CallChainTracer(exact_store, settings)
        self.expander = Expander(
            settings=settings,
            exact_store=exact_store,
            relation_store=relation_store,
            chain=self.chain,
        )

    def _has_fallback_recall(self, state: RetrievalState) -> bool:
        return self.recall.has_fallback_recall(state)

    async def search(
        self,
        query: str,
        scope: SearchScope | None = None,
        *,
        audit: RetrievalAudit | None = None,
    ) -> list[SearchHit]:
        """One retrieval: the primary hits in final order, then relation excerpts.

        ``scope`` is the resolved workspace; None means no filtering and is
        only for tests. ``audit`` collects stage timings when given and costs
        nothing otherwise.
        """
        state = RetrievalState(query=query, scope=scope, audit=audit)
        if audit is not None:
            audit.scope_size = (
                len(state.allowed_blob_names)
                if state.allowed_blob_names is not None
                else None
            )
        # None means unfiltered; an empty scope has nothing to search.
        if state.allowed_blob_names is not None and not state.allowed_blob_names:
            return []

        self.route(state)
        # SQL lanes need only routing, so they overlap the remote query
        # embedding instead of waiting behind it.
        sql_lanes = self.recall.start_sql_lanes(state)
        try:
            await self.plan.plan(state)
            await self.recall.recall(state, sql_lanes)
        except BaseException:
            for task in sql_lanes:
                task.cancel()
            # Merely cancelling background tasks leaves their exceptions and
            # database contexts pending. Drain every lane before propagating
            # planning failures or caller cancellation. The embedding request
            # is released instead of cancelled (see ``release_embedding``).
            await asyncio.gather(*sql_lanes, return_exceptions=True)
            release_embedding(state)
            raise
        await self.fusion.fuse(state)
        if not state.candidates:
            return []
        await self.ranker.rank(state)
        await self.select(state)
        await self.expander.expand(state)
        return [*state.selected, *state.related]

    def route(self, state: RetrievalState) -> None:
        """Routing is deterministic; models only take part in explicit rewrite/rerank."""
        state.evidence = extract_query_evidence(state.query)
        state.lookup_identifiers, state.qualifiers = split_qualified_identifiers(
            state.evidence.identifiers
        )
        state.intent = classify_query_intent(state.query)
        state.strategy = get_strategy(state.intent)
        logger.debug(
            "Query intent: {}, strategy: {}", state.intent.value, state.strategy
        )
        # The path index answers "which file", the content index "which part
        # of it"; both recall only for file-locating requests and merge at
        # chunk granularity.
        state.use_path_index = self.path_store is not None and (
            state.strategy.enable_path_index
            or should_use_path_index(state.query, state.intent)
        )
        if state.audit is not None:
            state.audit.intent = state.intent.value
            state.audit.path_boosted = state.use_path_index

    async def select(self, state: RetrievalState) -> None:
        # Primary selection uses the full budget: relation evidence is
        # optional and only earns room once a lookup returns something new.
        with state.stage("select"):
            state.selected = await self.selector.select(
                state.candidates,
                self.settings.final_select_k,
                mode=state.strategy.selection_mode,
                max_chars=context_budget(self.settings, state),
            )
