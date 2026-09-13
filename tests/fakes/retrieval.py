"""In-memory stores and embedders for exercising ``RetrievalPipeline``.

The fake embedder registers each query text under the vector it returns, so
the fake search store can answer per query even though the pipeline only
ever hands it a vector.
"""

from __future__ import annotations

from collections.abc import Sequence

from oce.domain.services.path_search import PathSearchResult
from oce.domain.services.search import (
    DefinitionHit,
    SearchHit,
    SearchScope,
    VectorRecord,
)

_TEXT_BY_VECTOR: dict[tuple[float, ...], str] = {}


class FakeEmbedder:
    """Deterministic query embedder that remembers the texts it was given."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        vector = [float(len(text)), float(sum(map(ord, text)) % 9973)]
        _TEXT_BY_VECTOR[tuple(vector)] = text
        return vector

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [await self.embed_query(text) for text in texts]


class FakeSearchStore:
    """``SearchStore`` returning preset hits, optionally per query text."""

    def __init__(self, hits: list[SearchHit] | None = None) -> None:
        self.hits = hits or []
        self.last_query: str = ""
        self.last_vector: list[float] = []
        self.queries: list[str] = []
        self.hits_by_query: dict[str, list[SearchHit]] = {}
        self.last_kwargs: dict[str, object] = {}
        self.upserted: list[VectorRecord] = []
        self.upsert_batch_sizes: list[int] = []
        self.deleted: list[str] = []

    async def search(
        self,
        *,
        query_vector: list[float],
        allowed_blob_names: Sequence[str] | None = None,
        top_k: int = 50,
        vector_threshold: float = 0.0,
    ) -> list[SearchHit]:
        self.last_kwargs = {
            "query_vector": query_vector,
            "allowed_blob_names": allowed_blob_names,
            "top_k": top_k,
            "vector_threshold": vector_threshold,
        }
        query = _TEXT_BY_VECTOR.get(tuple(query_vector), "")
        self.last_query = query
        self.last_vector = query_vector
        self.queries.append(query)
        return list(self.hits_by_query.get(query, self.hits))

    async def upsert(self, records: Sequence[VectorRecord]) -> None:
        self.upsert_batch_sizes.append(len(records))
        self.upserted.extend(records)

    async def delete(self, blob_names: Sequence[str]) -> None:
        self.deleted.extend(blob_names)


class FakePathStore:
    def __init__(self, results: list[PathSearchResult] | None = None) -> None:
        self.queries = 0
        self.results = results or []

    async def search_paths(
        self, query_vector, allowed_blob_names=None, top_k: int = 20
    ) -> list[PathSearchResult]:
        self.queries += 1
        return list(self.results)


class FakePathContentStore:
    def __init__(
        self, hits: list[SearchHit] | None = None, error: Exception | None = None
    ) -> None:
        self.hits = hits or []
        self.error = error
        self.blob_names: tuple[str, ...] = ()

    async def get_representative_chunks(self, blob_names) -> list[SearchHit]:
        self.blob_names = tuple(blob_names)
        if self.error is not None:
            raise self.error
        return list(self.hits)


class FakeExactSearchStore:
    """``ExactSearchStore`` returning preset occurrences and no definitions."""

    def __init__(
        self, hits: list[SearchHit] | None = None, error: Exception | None = None
    ) -> None:
        self.hits = hits or []
        self.error = error
        self.identifiers: tuple[str, ...] = ()
        self.scope: SearchScope | None = None
        self.kinds: tuple[str, ...] | None = None
        self.kinds_seen: list[tuple[str, ...] | None] = []

    async def search_exact(
        self, *, identifiers, scope, top_k: int = 50, kinds=None
    ) -> list[SearchHit]:
        self.identifiers = tuple(identifiers)
        self.scope = scope
        self.kinds = kinds
        self.kinds_seen.append(kinds)
        if self.error is not None:
            raise self.error
        return list(self.hits[:top_k])

    async def find_definitions(
        self, *, identifiers, scope, max_per_identifier: int = 3, enclosing=None
    ) -> list[DefinitionHit]:
        return []


class FakeLexicalStore:
    def __init__(self, hits: list[SearchHit] | None = None) -> None:
        self.hits = hits or []
        self.calls: list[dict] = []

    async def search_lexical(
        self, *, terms, phrases, scope, top_k: int = 30, required=()
    ) -> list[SearchHit]:
        self.calls.append(
            {"terms": terms, "phrases": phrases, "top_k": top_k, "required": required}
        )
        return list(self.hits)


class FakePathLookupStore:
    def __init__(self, scores: dict[str, float] | None = None) -> None:
        self.scores = scores or {}
        self.calls: list[dict] = []

    async def match_paths(
        self, *, filenames, paths, scope, limit: int = 20
    ) -> dict[str, float]:
        self.calls.append({"filenames": filenames, "paths": paths})
        return dict(self.scores)
