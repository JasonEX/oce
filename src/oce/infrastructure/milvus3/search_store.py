"""SearchStore / VectorIndex implementation over the content collection."""

from __future__ import annotations

from collections.abc import Sequence

from oce.domain.services.search import SearchHit, VectorRecord
from oce.infrastructure.milvus3.client import Milvus3Client
from oce.shared.config.settings import MilvusSettings
from oce.shared.index_stats import IndexStoreStats


class Milvus3SearchStore:
    def __init__(self, milvus_settings: MilvusSettings, *, dense_dim: int):
        self.client = Milvus3Client(milvus_settings, dense_dim=dense_dim)
        self.milvus_settings = milvus_settings

    async def search(
        self,
        *,
        query_vector: list[float],
        allowed_blob_names: Sequence[str] | None = None,
        top_k: int = 50,
        vector_threshold: float = 0.0,
    ) -> list[SearchHit]:
        """Dense search inside the workspace scope; an empty scope searches nothing."""
        if allowed_blob_names is not None and not allowed_blob_names:
            return []
        hits = await self.client.search(
            query_vector,
            blob_filter=list(allowed_blob_names) if allowed_blob_names else None,
            top_k=top_k,
        )
        return [hit for hit in hits if hit.score >= vector_threshold]

    async def upsert(self, records: Sequence[VectorRecord]) -> None:
        await self.client.insert(records)

    async def delete(self, blob_names: Sequence[str]) -> None:
        await self.client.delete_by_blob_names(blob_names)

    async def index_stats(self) -> IndexStoreStats:
        return await self.client.index_stats()

    async def has_index_data(self) -> bool:
        return await self.client.has_index_data()

    async def close(self) -> None:
        await self.client.close()
