"""Content chunk collection: vectors plus the fields a SearchHit needs."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from loguru import logger
from pymilvus import CollectionSchema

from oce.domain.services.search import SearchHit, VectorRecord
from oce.infrastructure.milvus3.base import MilvusCollectionClient, build_blob_filter
from oce.infrastructure.milvus3.schema import create_oce_collection_schema
from oce.shared.config.settings import MilvusSettings

_MAX_CONTENT_BYTES = 65_535
_OUTPUT_FIELDS = ["chunk_id", "content_hash", "content", "blob_name", "metadata"]


def _fit_content_field(content: str) -> tuple[str, bool]:
    """Fit text into Milvus VARCHAR without splitting a UTF-8 code point."""
    encoded = content.encode("utf-8")
    if len(encoded) <= _MAX_CONTENT_BYTES:
        return content, False
    return encoded[:_MAX_CONTENT_BYTES].decode("utf-8", errors="ignore"), True


class Milvus3Client(MilvusCollectionClient):
    def __init__(self, settings: MilvusSettings, *, dense_dim: int) -> None:
        super().__init__(
            settings,
            collection_name=settings.collection_name,
            dense_dim=dense_dim,
            vector_field="dense_vector",
        )

    def _build_schema(self) -> CollectionSchema:
        return create_oce_collection_schema(dense_dim=self.dense_dim)

    async def insert(self, records: Sequence[VectorRecord]) -> int:
        """Upsert chunk vectors; returns the number of rows written."""
        if not records:
            return 0
        rows: list[dict[str, Any]] = []
        for record in records:
            content, truncated = _fit_content_field(record.content)
            metadata: dict[str, Any] = {
                "path": record.path,
                "start_line": record.start_line,
                "end_line": record.end_line,
            }
            if truncated:
                metadata["content_truncated"] = True
                metadata["content_bytes"] = len(record.content.encode("utf-8"))
            rows.append(
                {
                    "chunk_id": record.chunk_id,
                    "content_hash": record.content_hash,
                    "content": content,
                    "dense_vector": record.vector,
                    "blob_name": record.blob_name,
                    "metadata": metadata,
                }
            )
        count = await self._upsert_rows(rows)
        logger.info("Upserted {} vectors", count)
        return count

    async def search(
        self,
        query_vector: list[float],
        blob_filter: list[str] | None = None,
        top_k: int = 10,
    ) -> list[SearchHit]:
        """Dense search returning hits in descending similarity order."""
        pairs = await self._search_vector(
            query_vector,
            filter_expr=build_blob_filter(blob_filter),
            top_k=top_k,
            output_fields=_OUTPUT_FIELDS,
        )
        hits: list[SearchHit] = []
        for entity, score in pairs:
            metadata = entity.get("metadata") or {}
            blob_name = entity.get("blob_name", "")
            hits.append(
                SearchHit(
                    blob_name=blob_name,
                    path=metadata.get("path", blob_name),
                    content=entity.get("content", ""),
                    score=score,
                    content_hash=entity.get("content_hash", ""),
                    start_line=metadata.get("start_line", 1),
                    end_line=metadata.get("end_line", 1),
                )
            )
        return hits

    async def delete_by_blob_names(self, blob_names: Sequence[str]) -> None:
        await self._delete_by_blob_names(list(blob_names))
        logger.info("Deleted vectors for {} blobs", len(blob_names))
