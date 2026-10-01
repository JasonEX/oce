"""Milvus-backed semantic path index."""

from __future__ import annotations

from typing import Any

from loguru import logger
from pymilvus import CollectionSchema

from oce.domain.services.path_search import PathSearchResult
from oce.infrastructure.milvus3.base import MilvusCollectionClient, build_blob_filter
from oce.infrastructure.milvus3.schema import create_path_collection_schema
from oce.shared.config.settings import MilvusSettings
from oce.shared.path_limits import PATH_DOCUMENT_PREVIEW_BYTES, PATH_PREVIEW_BYTES


def _utf8_prefix(value: str, max_bytes: int) -> str:
    """Fit a stored diagnostic without cutting a UTF-8 code point."""
    return value.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


class PathIndexClient(MilvusCollectionClient):
    def __init__(self, settings: MilvusSettings, *, dense_dim: int) -> None:
        super().__init__(
            settings,
            collection_name=settings.path_collection_name,
            dense_dim=dense_dim,
            vector_field="path_vector",
        )

    def _build_schema(self) -> CollectionSchema:
        return create_path_collection_schema(self.dense_dim)

    async def insert(self, path_docs: list[dict[str, Any]]) -> dict[str, Any]:
        """Upsert path documents in bounded batches."""
        if not path_docs:
            return {"inserted": 0}
        rows = [
            {
                "path_id": doc["path_id"],
                "blob_name": doc["blob_name"],
                "path": _utf8_prefix(doc["path"], PATH_PREVIEW_BYTES),
                "path_document": _utf8_prefix(
                    doc["path_document"], PATH_DOCUMENT_PREVIEW_BYTES
                ),
                "path_vector": doc["path_vector"],
            }
            for doc in path_docs
        ]
        count = await self._upsert_rows(rows)
        logger.info("Upserted {} path documents", count)
        return {"inserted": count}

    async def search_paths(
        self,
        query_vector: list[float],
        allowed_blob_names: list[str] | None = None,
        top_k: int = 20,
    ) -> list[PathSearchResult]:
        """Search semantic path documents within the resolved workspace scope."""
        if allowed_blob_names is not None and not allowed_blob_names:
            return []
        # Validate the scope before touching the connection.
        filter_expr = build_blob_filter(allowed_blob_names) or ""
        pairs = await self._search_vector(
            query_vector,
            filter_expr=filter_expr,
            top_k=top_k,
            output_fields=["blob_name"],
        )
        hits = [
            PathSearchResult(
                blob_name=entity.get("blob_name") or "",
                score=score,
            )
            for entity, score in pairs
        ]
        logger.debug("Path index search returned {} results", len(hits))
        return hits

    async def delete_by_blob_names(self, blob_names: list[str]) -> None:
        await self._delete_by_blob_names(blob_names)
        logger.info("Deleted path documents for {} blobs", len(blob_names))
