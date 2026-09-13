"""The semantic path index used by file-name requests."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from oce.domain.services.search import SearchHit


@dataclass(frozen=True)
class PathSearchResult:
    """One path index hit."""

    path: str
    blob_name: str
    score: float


class PathSearchStore(Protocol):
    """Search and write side of the path index."""

    async def search_paths(
        self,
        query_vector: list[float],
        allowed_blob_names: list[str] | None = None,
        top_k: int = 20,
    ) -> list[PathSearchResult]:
        """Path documents nearest to ``query_vector`` inside the allowed blobs."""
        ...

    async def insert(self, path_docs: list[dict[str, Any]]) -> dict[str, Any]:
        """Upsert path documents (path_id, blob_name, path, path_document, path_vector)."""
        ...

    async def delete_by_blob_names(self, blob_names: list[str]) -> None:
        """Remove the path documents of the given blobs."""
        ...


class PathContentStore(Protocol):
    """Resolve representative source chunks for path-only recall hits."""

    async def get_representative_chunks(
        self,
        blob_names: Sequence[str],
    ) -> list[SearchHit]: ...
