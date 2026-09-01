"""Milvus-backed semantic path index."""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger
from pymilvus import AsyncMilvusClient, MilvusClient
from pymilvus.client.types import LoadState

from oce.domain.services.path_search import PathSearchResult
from oce.shared.config.settings import MilvusSettings
from oce.shared.index_stats import IndexStoreStats

from .client import build_blob_filter, validate_blob_name
from .schema import create_path_collection_schema


class PathIndexClient:
    """Own the path collection and expose non-blocking search operations."""

    def __init__(self, settings: MilvusSettings) -> None:
        self.settings = settings
        self.collection_name = settings.path_collection_name
        self.dense_dim = settings.dense_dim
        self._local = not settings.endpoint.startswith(("http://", "https://"))
        client_type = MilvusClient if self._local else AsyncMilvusClient
        self._client = client_type(uri=settings.endpoint, token=settings.token)
        self._initialize_lock = asyncio.Lock()
        self._initialized = False
        self._closed = False

    async def _call(self, method_name: str, *args, **kwargs):
        """Use native async I/O remotely and a worker thread for Milvus Lite."""
        if self._closed and method_name != "close":
            raise RuntimeError("Path index client is closed")
        method = getattr(self._client, method_name)
        if self._local:
            return await asyncio.to_thread(method, *args, **kwargs)
        return await method(*args, **kwargs)

    async def initialize(self) -> None:
        """Create or load the collection exactly once per client lifecycle."""
        if self._closed:
            raise RuntimeError("Path index client is closed")
        if self._initialized:
            return
        async with self._initialize_lock:
            if self._closed:
                raise RuntimeError("Path index client is closed")
            if self._initialized:
                return
            await self._ensure_collection()
            self._initialized = True
            logger.info(
                "PathIndexClient initialized, collection: {}", self.collection_name
            )

    async def _ensure_collection(self) -> None:
        if not await self._call("has_collection", self.collection_name):
            await self._call(
                "create_collection",
                collection_name=self.collection_name,
                schema=create_path_collection_schema(self.dense_dim),
            )
        await self._ensure_vector_index()
        state = await self._call("get_load_state", self.collection_name)
        load_state = state.get("state") if isinstance(state, dict) else state
        if load_state != LoadState.Loaded:
            await self._call("load_collection", self.collection_name)

    async def _ensure_vector_index(self) -> None:
        indexes = await self._call(
            "list_indexes",
            self.collection_name,
            field_name="path_vector",
        )
        if indexes:
            return
        try:
            await self._call(
                "create_index",
                collection_name=self.collection_name,
                index_params=self._build_index_params(),
            )
        except Exception as exc:
            if not self._local:
                raise RuntimeError(
                    f"Failed to create Milvus path index for {self.collection_name}"
                ) from exc
            logger.warning("Milvus Lite did not create the path index: {}", exc)

    def _build_index_params(self):
        index_params = self._client.prepare_index_params()
        index_params.add_index(
            field_name="path_vector",
            index_type=self.settings.dense_index_type,
            metric_type=self.settings.dense_metric_type,
            params={
                "M": self.settings.hnsw_m,
                "efConstruction": self.settings.hnsw_ef_construction,
            },
        )
        return index_params

    async def insert(self, path_docs: list[dict[str, Any]]) -> dict[str, Any]:
        """Upsert path documents in bounded batches."""
        if not path_docs:
            return {"inserted": 0}
        await self.initialize()
        data = [
            {
                "path_id": doc["path_id"],
                "blob_name": doc["blob_name"],
                "path": doc["path"],
                "path_document": doc["path_document"],
                "path_vector": doc["path_vector"],
            }
            for doc in path_docs
        ]

        count = 0
        batch_size = 1000
        for start in range(0, len(data), batch_size):
            batch = data[start : start + batch_size]
            result = await self._call(
                "upsert",
                collection_name=self.collection_name,
                data=batch,
            )
            count += result.get("upsert_count", result.get("insert_count", len(batch)))
        logger.info("Upserted {} path documents", count)
        return {"inserted": count}

    async def search_paths(
        self,
        query_vector: list[float],
        allowed_blob_names: list[str] | None = None,
        top_k: int = 20,
    ) -> list[PathSearchResult]:
        """Search semantic path documents within the resolved workspace scope."""
        filter_expr = build_blob_filter(allowed_blob_names) or ""
        await self.initialize()

        results = await self._call(
            "search",
            collection_name=self.collection_name,
            data=[query_vector],
            anns_field="path_vector",
            search_params={
                "metric_type": self.settings.dense_metric_type,
                "params": {"ef": max(self.settings.hnsw_ef_search, top_k * 2)},
            },
            limit=top_k,
            filter=filter_expr,
            output_fields=["blob_name", "path"],
        )

        hits: list[PathSearchResult] = []
        if results:
            for result in results[0]:
                entity = (
                    result.get("entity", result)
                    if isinstance(result, dict)
                    else result.entity
                )
                score = (
                    result.get("distance", result.get("score", 0.0))
                    if isinstance(result, dict)
                    else result.distance
                )
                hits.append(
                    PathSearchResult(
                        path=entity.get("path"),
                        blob_name=entity.get("blob_name"),
                        score=float(score),
                    )
                )
        logger.debug("Path index search returned {} results", len(hits))
        return hits

    async def delete_by_blob_names(self, blob_names: list[str]) -> None:
        """Delete path documents for validated blob identifiers."""
        validated_blob_names = [validate_blob_name(name) for name in blob_names]
        await self.initialize()
        quoted = ", ".join(f'"{name}"' for name in validated_blob_names)
        await self._call(
            "delete",
            collection_name=self.collection_name,
            filter=f"blob_name in [{quoted}]",
        )
        logger.info("Deleted path documents for {} blobs", len(validated_blob_names))

    async def _read_collection_stats(self) -> tuple[bool, int]:
        if not await self._call("has_collection", self.collection_name):
            return False, 0
        stats = await self._call("get_collection_stats", self.collection_name)
        return True, int(stats.get("row_count", 0))

    async def index_stats(self) -> IndexStoreStats:
        """Report initialized state without creating or loading a collection."""
        if not self._initialized:
            return IndexStoreStats(
                enabled=True,
                available=False,
                collection_name=self.collection_name,
                error_type="NotInitialized",
            )
        exists, entities = await self._read_collection_stats()
        return IndexStoreStats(
            enabled=True,
            available=True,
            collection_name=self.collection_name,
            exists=exists,
            entities=entities,
        )

    async def has_index_data(self) -> bool:
        """Probe existing rows without creating or loading the collection."""
        exists, entities = await self._read_collection_stats()
        return exists and entities > 0

    async def close(self) -> None:
        """Move the instance-owned client into its terminal closed state."""
        if self._closed:
            return
        async with self._initialize_lock:
            if self._closed:
                return
            await self._call("close")
            self._initialized = False
            self._closed = True
