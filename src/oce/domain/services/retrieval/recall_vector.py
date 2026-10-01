"""The embedding-backed lanes: dense chunk search and the semantic path index.

Both need the query vectors, so both wait on the embedding task the plan
stage started. Whether they are awaited at all is the recall orchestrator's
decision; when they are, a failure degrades to an empty list as long as
another lane can still answer.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from time import perf_counter

from loguru import logger

from oce.domain.services.path_search import PathSearchStore
from oce.domain.services.retrieval.fuse import fuse_lists
from oce.domain.services.retrieval.state import RetrievalState, lane_failed
from oce.domain.services.search import SearchHit, SearchStore
from oce.shared.aio import wait_released
from oce.shared.config.settings import RetrievalSettings


@dataclass
class VectorRecall:
    """What the vector lanes returned, and the stage timings behind it.

    The timings are committed to the audit only when the result is used; a
    lane that raced ahead and was dropped leaves no trace of work the answer
    never depended on.
    """

    dense: list[SearchHit] = field(default_factory=list)
    dense_error: Exception | None = None
    path_scores: dict[str, float] = field(default_factory=dict)
    stages: dict[str, int] = field(default_factory=dict)

    @contextmanager
    def timed(self, name: str) -> Iterator[None]:
        start = perf_counter()
        try:
            yield
        finally:
            elapsed = int((perf_counter() - start) * 1000)
            self.stages[name] = self.stages.get(name, 0) + elapsed


class VectorLanes:
    def __init__(
        self,
        *,
        store: SearchStore,
        path_store: PathSearchStore | None,
        settings: RetrievalSettings,
    ) -> None:
        self.store = store
        self.path_store = path_store
        self.settings = settings

    async def recall(
        self, state: RetrievalState, *, has_fallback: bool
    ) -> VectorRecall:
        """Await the query vectors, then run both lanes.

        ``has_fallback`` says whether a SQL lane can still answer; it decides
        between degrading to an empty result and raising when the embedding
        or the dense search fails.
        """
        result = VectorRecall()
        vectors = await self.await_embedding(state, result, has_fallback=has_fallback)
        if vectors is None:
            return result
        (result.dense, result.dense_error), result.path_scores = await asyncio.gather(
            self.recall_dense(state, vectors, result, has_fallback=has_fallback),
            self.recall_paths(state, vectors, result),
        )
        return result

    async def await_embedding(
        self, state: RetrievalState, result: VectorRecall, *, has_fallback: bool
    ) -> dict[str, list[float]] | None:
        """The query vectors, or None after a failure another lane can absorb."""
        if state.embedding is None:
            return {}
        try:
            # Cancelling the vector lanes releases this wait; it never
            # cancels the request itself (see ``release_embedding``).
            vectors, elapsed_ms = await wait_released(state.embedding)
        except Exception as exc:
            if not has_fallback:
                raise
            lane_failed(state, "embed", exc)
            result.dense_error = exc
            return None
        result.stages["embed"] = elapsed_ms
        return vectors

    async def recall_dense(
        self,
        state: RetrievalState,
        vectors: dict[str, list[float]],
        result: VectorRecall,
        *,
        has_fallback: bool,
    ) -> tuple[list[SearchHit], Exception | None]:
        try:
            with result.timed("dense"):
                result_lists = await asyncio.gather(
                    *(
                        self.recall_with_vector(
                            vectors[planned_query],
                            state.allowed_blob_names,
                            num_queries,
                        )
                        for planned_query, num_queries in state.plan.facets
                    )
                )
            return fuse_lists(self.settings, list(result_lists)), None
        except Exception as exc:
            if not state.route.use_path_index and not has_fallback:
                raise
            lane_failed(state, "dense", exc)
            return [], exc

    async def recall_with_vector(
        self,
        query_vector: list[float],
        allowed_blob_names: set[str] | frozenset[str] | None,
        num_queries: int = 1,
    ) -> list[SearchHit]:
        # One query recalls the full window; facets of a decomposed request
        # each recall a smaller one and are fused afterwards.
        top_k = (
            self.settings.default_top_k
            if num_queries == 1
            else self.settings.per_query_top_k
        )
        return await self.store.search(
            query_vector=query_vector,
            allowed_blob_names=(
                sorted(allowed_blob_names) if allowed_blob_names is not None else None
            ),
            top_k=top_k,
            vector_threshold=self.settings.vector_threshold,
        )

    async def recall_paths(
        self,
        state: RetrievalState,
        vectors: dict[str, list[float]],
        result: VectorRecall,
    ) -> dict[str, float]:
        """Best path score per blob over every query variant; failures degrade to none."""
        if not state.route.use_path_index or self.path_store is None:
            return {}
        allowed = state.allowed_blob_names
        blob_filter = list(allowed) if allowed else None
        path_scores: dict[str, float] = {}
        try:
            with result.timed("path"):
                result_lists = await asyncio.gather(
                    *(
                        self.path_store.search_paths(
                            query_vector=vectors[variant],
                            allowed_blob_names=blob_filter,
                            top_k=self.settings.path_top_k,
                        )
                        for variant in state.plan.path_queries
                    )
                )
        except Exception as exc:
            lane_failed(state, "path", exc)
            return path_scores
        for path_results in result_lists:
            for item in path_results:
                if item.score > path_scores.get(item.blob_name, float("-inf")):
                    path_scores[item.blob_name] = item.score
        logger.info("Path index returned {} results", len(path_scores))
        return path_scores
