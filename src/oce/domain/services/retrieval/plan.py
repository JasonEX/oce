"""The plan stage: rewrite, facet decomposition and the query embedding task.

The embedding round trip is started here and never awaited here. Recall
decides whether the answer needs it; ``release_embedding`` lets an unneeded
request finish on its own instead of cancelling it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from time import perf_counter
from typing import TYPE_CHECKING

from loguru import logger

from oce.domain.services.embedder import Embedder
from oce.domain.services.query_planner import QueryPlanner
from oce.domain.services.retrieval.state import (
    EmbeddingResult,
    QueryPlan,
    RetrievalState,
)

if TYPE_CHECKING:
    from oce.domain.services.llm.rewriter import QueryRewriter


def _swallow_task_result(task: asyncio.Task[object]) -> None:
    """Consume the outcome of a released background task so nothing is left pending."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.debug("Released query embedding failed: {}", type(exc).__name__)


def release_embedding(state: RetrievalState) -> None:
    """Let an unneeded embedding request finish on its own.

    Cancelling an in-flight HTTP request mid-response can leave its pooled
    connection unreturned; after enough skips the client blocks on the
    pool and every later request times out. The request is cheap to let
    complete, it fills the query-vector cache, and nothing waits for it.
    """
    task = state.embedding
    if task is None:
        return
    if task.done():
        _swallow_task_result(task)
        return
    task.add_done_callback(_swallow_task_result)


class Planner:
    """Turn the routed request into the query variants the lanes search with."""

    def __init__(
        self,
        *,
        embedder: Embedder,
        query_planner: QueryPlanner,
        query_rewriter: QueryRewriter | None = None,
    ) -> None:
        self.embedder = embedder
        self.query_planner = query_planner
        self.query_rewriter = query_rewriter

    async def plan(self, state: RetrievalState) -> None:
        # The rewriter is fault tolerant: on failure it returns the original
        # query rather than raising.
        queries: tuple[str, ...] = (state.query,)
        if (
            state.route.strategy.enable_query_rewrite
            and self.query_rewriter is not None
        ):
            with state.stage("rewrite"):
                rewritten = await self.query_rewriter.rewrite(state.query)
            if rewritten:
                queries = tuple(rewritten)

        facets = self._plan_queries(queries)
        # The path index searches the original query and every rewrite: a
        # Chinese request embedded directly rarely matches an English path
        # document, while a rewrite that names the file (CHANGES.rst) does.
        path_queries = (
            tuple(dict.fromkeys((state.query, *queries)))
            if state.route.use_path_index
            else ()
        )
        state.plan = QueryPlan(
            queries=queries, facets=facets, path_queries=path_queries
        )
        # Started, not awaited: recall decides whether the answer needs it.
        state.embedding = asyncio.create_task(
            self._embed_query_vectors([*path_queries, *(item[0] for item in facets)])
        )

    def _plan_queries(self, queries: Sequence[str]) -> tuple[tuple[str, int], ...]:
        planned: list[tuple[str, int]] = []
        for query in queries:
            facets = self.query_planner.plan(query)
            count = len(facets)
            planned.extend((facet, count) for facet in facets)
        return tuple(planned)

    async def _embed_query_vectors(self, queries: Sequence[str]) -> EmbeddingResult:
        """Query vectors plus the wall time of the round trip in milliseconds."""
        started = perf_counter()
        unique_queries = tuple(dict.fromkeys(queries))
        vectors = await asyncio.gather(
            *(self.embedder.embed_query(query) for query in unique_queries)
        )
        elapsed_ms = int((perf_counter() - started) * 1000)
        return dict(zip(unique_queries, vectors, strict=True)), elapsed_ms
