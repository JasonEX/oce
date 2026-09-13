"""The embedding-backed lanes: dense chunk search and the semantic path index.

Both need the query vectors, so both wait on the embedding task the plan
stage started. Whether they are awaited at all is the recall orchestrator's
decision; when they are, a failure degrades to an empty list as long as
another lane can still answer.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from loguru import logger

from oce.domain.services.path_search import PathSearchStore
from oce.domain.services.retrieval.fuse import fuse_lists
from oce.domain.services.retrieval.state import RetrievalState, lane_failed
from oce.domain.services.search import SearchHit, SearchStore
from oce.shared.config.settings import RetrievalSettings


class VectorLanes:
    def __init__(
        self,
        *,
        store: SearchStore,
        path_store: PathSearchStore | None,
        settings: RetrievalSettings,
        has_fallback: Callable[[RetrievalState], bool],
    ) -> None:
        self.store = store
        self.path_store = path_store
        self.settings = settings
        # Whether a SQL lane can still answer when the embedding or the dense
        # search fails; decides between degrading and raising.
        self._has_fallback = has_fallback

    async def recall(
        self, state: RetrievalState
    ) -> tuple[tuple[list[SearchHit], Exception | None], dict[str, float]]:
        await self.await_embedding(state)
        return await asyncio.gather(self.recall_dense(state), self.recall_paths(state))

    async def await_embedding(self, state: RetrievalState) -> None:
        if state.embed_task is None:
            return
        try:
            # Shielded: cancelling the vector lanes must not cancel the request.
            state.vectors, elapsed_ms = await asyncio.shield(state.embed_task)
        except Exception as exc:
            if not self._has_fallback(state):
                raise
            lane_failed(state, "embed", exc)
            state.embed_error = exc
            return
        state.vector_stages["embed"] = elapsed_ms

    async def recall_dense(
        self, state: RetrievalState
    ) -> tuple[list[SearchHit], Exception | None]:
        if state.embed_error is not None:
            return [], state.embed_error
        try:
            with state.vector_stage("dense"):
                result_lists = await asyncio.gather(
                    *(
                        self.recall_with_vector(
                            state.vectors[planned_query],
                            state.allowed_blob_names,
                            num_queries,
                        )
                        for planned_query, num_queries in state.planned
                    )
                )
            return fuse_lists(self.settings, list(result_lists)), None
        except Exception as exc:
            if not state.use_path_index and not self._has_fallback(state):
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

    async def recall_paths(self, state: RetrievalState) -> dict[str, float]:
        """Best path score per blob over every query variant; failures degrade to none."""
        if (
            not state.use_path_index
            or self.path_store is None
            or state.embed_error is not None
        ):
            return {}
        allowed = state.allowed_blob_names
        blob_filter = list(allowed) if allowed else None
        path_scores: dict[str, float] = {}
        try:
            with state.vector_stage("path"):
                result_lists = await asyncio.gather(
                    *(
                        self.path_store.search_paths(
                            query_vector=state.vectors[variant],
                            allowed_blob_names=blob_filter,
                            top_k=self.settings.path_top_k,
                        )
                        for variant in state.path_queries
                    )
                )
        except Exception as exc:
            lane_failed(state, "path", exc)
            return path_scores
        for path_results in result_lists:
            for result in path_results:
                if result.score > path_scores.get(result.blob_name, float("-inf")):
                    path_scores[result.blob_name] = result.score
        logger.info("Path index returned {} results", len(path_scores))
        return path_scores
