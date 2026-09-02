"""One Milvus collection: connection, lifecycle, index, and vector search."""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger
from pymilvus import AsyncMilvusClient, CollectionSchema, MilvusClient
from pymilvus.client.types import LoadState

from oce.shared.config.settings import MilvusSettings


def validate_blob_name(blob_name: str) -> str:
    if len(blob_name) != 64 or any(
        character not in "0123456789abcdef" for character in blob_name.casefold()
    ):
        raise ValueError("Milvus blob filters require a SHA256 blob_name")
    return blob_name


def build_blob_filter(blob_names: list[str] | None) -> str | None:
    """Build the production Milvus workspace filter from validated blob names."""
    if not blob_names:
        return None
    blob_list = ", ".join(
        f'"{validate_blob_name(blob_name)}"' for blob_name in blob_names
    )
    return f"blob_name in [{blob_list}]"


class MilvusCollectionClient:
    """Own one collection on a remote Milvus or an embedded Milvus Lite file.

    Remote endpoints use the native async client; Milvus Lite only ships a sync
    client, so its calls run in a worker thread. Subclasses supply the schema and
    the vector field; everything else about a collection's lifecycle is shared.
    """

    def __init__(
        self,
        settings: MilvusSettings,
        *,
        collection_name: str,
        dense_dim: int,
        vector_field: str,
    ) -> None:
        self.settings = settings
        self.collection_name = collection_name
        self.dense_dim = dense_dim
        self._vector_field = vector_field
        self._local = not settings.endpoint.startswith(("http://", "https://"))
        client_type = MilvusClient if self._local else AsyncMilvusClient
        self._client = client_type(uri=settings.endpoint, token=settings.token)
        self._initialize_lock = asyncio.Lock()
        self._initialized = False
        self._closed = False
        logger.info("Connecting to Milvus: {}", settings.endpoint)

    def _build_schema(self) -> CollectionSchema:
        raise NotImplementedError

    async def _call(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        if self._closed and method_name != "close":
            raise RuntimeError(f"Milvus client for {self.collection_name} is closed")
        method = getattr(self._client, method_name)
        if self._local:
            return await asyncio.to_thread(method, *args, **kwargs)
        return await method(*args, **kwargs)

    async def initialize(self) -> None:
        """Create or load the collection exactly once per client lifecycle."""
        if self._closed:
            raise RuntimeError(f"Milvus client for {self.collection_name} is closed")
        if self._initialized:
            return
        async with self._initialize_lock:
            if self._initialized:
                return
            await self._ensure_collection()
            self._initialized = True
            logger.info("Milvus collection ready: {}", self.collection_name)

    async def _ensure_collection(self) -> None:
        if not await self._call("has_collection", self.collection_name):
            logger.info("Creating Milvus collection: {}", self.collection_name)
            await self._call(
                "create_collection",
                collection_name=self.collection_name,
                schema=self._build_schema(),
            )
        await self._ensure_vector_index()
        # Milvus Lite 的 load 状态不跨进程持久，重启后 collection 回到 released，
        # 必须重新 load 才能 search（load_collection 幂等）。
        state = await self._call("get_load_state", self.collection_name)
        load_state = state.get("state") if isinstance(state, dict) else state
        if load_state != LoadState.Loaded:
            await self._call("load_collection", self.collection_name)

    async def _ensure_vector_index(self) -> None:
        indexes = await self._call(
            "list_indexes",
            self.collection_name,
            field_name=self._vector_field,
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
                    f"Failed to create Milvus index for {self.collection_name}"
                ) from exc
            logger.warning("Milvus Lite did not create the vector index: {}", exc)

    def _build_index_params(self):
        index_params = self._client.prepare_index_params()
        index_params.add_index(
            field_name=self._vector_field,
            index_type=self.settings.dense_index_type,
            metric_type=self.settings.dense_metric_type,
            params={
                "M": self.settings.hnsw_m,
                "efConstruction": self.settings.hnsw_ef_construction,
            },
        )
        return index_params

    async def _upsert_rows(
        self, rows: list[dict[str, Any]], batch_size: int = 1000
    ) -> int:
        await self.initialize()
        count = 0
        for start in range(0, len(rows), batch_size):
            batch = rows[start : start + batch_size]
            result = await self._call(
                "upsert",
                collection_name=self.collection_name,
                data=batch,
            )
            count += result.get("upsert_count", result.get("insert_count", len(batch)))
        return count

    async def _search_vector(
        self,
        query_vector: list[float],
        *,
        filter_expr: str | None,
        top_k: int,
        output_fields: list[str],
    ) -> list[tuple[dict[str, Any], float]]:
        """Run one dense search and return ``(entity, score)`` pairs for the top hits."""
        await self.initialize()
        results = await self._call(
            "search",
            collection_name=self.collection_name,
            data=[query_vector],
            anns_field=self._vector_field,
            search_params={
                "metric_type": self.settings.dense_metric_type,
                "params": {"ef": max(self.settings.hnsw_ef_search, top_k * 2)},
            },
            limit=top_k,
            filter=filter_expr,
            output_fields=output_fields,
        )
        if not results:
            return []
        parsed: list[tuple[dict[str, Any], float]] = []
        # Remote hits are objects; Milvus Lite returns plain dicts.
        for hit in results[0]:
            if isinstance(hit, dict):
                entity = hit.get("entity", hit)
                score = hit.get("distance", hit.get("score", 0.0))
            else:
                entity = hit.entity
                score = hit.distance
            parsed.append((entity, float(score)))
        return parsed

    async def _delete_by_blob_names(self, blob_names: list[str]) -> None:
        validated = [validate_blob_name(name) for name in blob_names]
        if not validated:
            return
        await self.initialize()
        quoted = ", ".join(f'"{name}"' for name in validated)
        await self._call(
            "delete",
            collection_name=self.collection_name,
            filter=f"blob_name in [{quoted}]",
        )

    async def read_collection_stats(self) -> tuple[bool, int]:
        """Read collection cardinality without creating or loading an index."""
        if not await self._call("has_collection", self.collection_name):
            return False, 0
        stats = await self._call("get_collection_stats", self.collection_name)
        return True, int(stats.get("row_count", 0))

    async def has_index_data(self) -> bool:
        """Probe existing rows without creating or loading the collection."""
        exists, entities = await self.read_collection_stats()
        return exists and entities > 0

    async def close(self) -> None:
        if self._closed:
            return
        async with self._initialize_lock:
            if self._closed:
                return
            await self._call("close")
            self._initialized = False
            self._closed = True
        logger.info("Milvus client closed: {}", self.collection_name)
