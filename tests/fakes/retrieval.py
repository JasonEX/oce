"""In-memory stores and embedders for exercising ``RetrievalPipeline``.

The fake embedder registers each query text under the vector it returns, so
the fake search store can answer per query even though the pipeline only
ever hands it a vector.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import replace
from types import MappingProxyType
from typing import Any

from oce.domain.services.path_search import PathSearchResult
from oce.domain.services.query_classifier import QueryIntent
from oce.domain.services.relations import RelatedOccurrence
from oce.domain.services.retrieval.route import route_query
from oce.domain.services.retrieval.state import RetrievalState
from oce.domain.services.retrieval_strategy import RetrievalStrategy, get_strategy
from oce.domain.services.search import (
    DefinitionHit,
    SearchHit,
    SearchScope,
    VectorRecord,
)
from oce.shared.metrics import RetrievalAudit

_TEXT_BY_VECTOR: dict[tuple[float, ...], str] = {}


class FakeReranker:
    """Exercise the authorized rerank stage without changing its input order."""

    async def rerank(self, query: str, hits: list[SearchHit]) -> list[SearchHit]:
        return hits


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
        self,
        hits: list[SearchHit] | None = None,
        error: Exception | None = None,
        *,
        definition_counts: Mapping[str, int] | None = None,
    ) -> None:
        self.hits = hits or []
        self.error = error
        self.identifiers: tuple[str, ...] = ()
        self.scope: SearchScope | None = None
        self.kinds: tuple[str, ...] | None = None
        self.kinds_seen: list[tuple[str, ...] | None] = []
        self.counts_by_identifier = dict(definition_counts or {})
        self.definition_count_requests: list[tuple[str, ...]] = []

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

    async def definition_counts(
        self, *, identifiers: Sequence[str], scope: SearchScope
    ) -> dict[str, int]:
        self.definition_count_requests.append(tuple(identifiers))
        return {
            name: self.counts_by_identifier[name]
            for name in identifiers
            if name in self.counts_by_identifier
        }

    async def occurrence_kinds(
        self,
        occurrences: Sequence[tuple[str, str]],
        scope: SearchScope,
    ) -> dict[tuple[str, str], frozenset[str]]:
        return {}

    async def calls_within(
        self,
        *,
        blob_name: str,
        start_line: int,
        end_line: int,
        scope: SearchScope,
    ) -> list[tuple[str, int, str]]:
        return []

    async def chunk_for_line(
        self, *, blob_name: str, line: int, scope: SearchScope
    ) -> SearchHit | None:
        return None


class ControlledExactSearchStore(FakeExactSearchStore):
    """SQL occurrences held until a test releases the structural lookup."""

    def __init__(
        self,
        hits: list[SearchHit] | None = None,
        *,
        use_sites: list[SearchHit] | None = None,
    ) -> None:
        super().__init__(hits)
        self.use_sites = use_sites
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()

    async def search_exact(
        self,
        *,
        identifiers: Sequence[str],
        scope: SearchScope,
        top_k: int = 50,
        kinds: Sequence[str] | None = None,
    ) -> list[SearchHit]:
        self.started.set()
        try:
            await self.release.wait()
            if kinds == ("call", "inherit") and self.use_sites is not None:
                return list(self.use_sites[:top_k])
            return await super().search_exact(
                identifiers=identifiers, scope=scope, top_k=top_k, kinds=kinds
            )
        finally:
            self.finished.set()


class FakeEvidenceStore(FakeExactSearchStore):
    """Recorded raw definition and call lookups for relation expansion."""

    def __init__(
        self,
        definitions: Sequence[DefinitionHit] = (),
        calls: dict[tuple[str, int, int], list[tuple[str, int, str]]] | None = None,
        *,
        call_error: Exception | None = None,
    ) -> None:
        super().__init__()
        self.definitions = list(definitions)
        self.calls = calls or {}
        self.call_error = call_error
        self.definition_requests: list[tuple[tuple[str, ...], int]] = []
        self.call_requests: list[tuple[str, int, int]] = []

    async def definition_counts(
        self, *, identifiers: Sequence[str], scope: SearchScope
    ) -> dict[str, int]:
        self.definition_count_requests.append(tuple(identifiers))
        return {
            name: len(
                {
                    (item.hit.blob_name, item.start_line)
                    for item in self.definitions
                    if item.identifier == name
                    and item.hit.blob_name in scope.blob_names
                }
            )
            for name in dict.fromkeys(identifiers)
        }

    async def find_definitions(
        self,
        *,
        identifiers: Sequence[str],
        scope: SearchScope,
        max_per_identifier: int = 3,
        enclosing: Sequence[str] | None = None,
    ) -> list[DefinitionHit]:
        self.definition_requests.append((tuple(identifiers), max_per_identifier))
        rows: list[DefinitionHit] = []
        for name in dict.fromkeys(identifiers):
            found = [
                item
                for item in self.definitions
                if item.identifier == name
                and item.hit.blob_name in scope.blob_names
                and (enclosing is None or item.enclosing in enclosing)
            ]
            if len(found) <= max_per_identifier:
                rows.extend(found)
        return rows

    async def calls_within(
        self,
        *,
        blob_name: str,
        start_line: int,
        end_line: int,
        scope: SearchScope,
    ) -> list[tuple[str, int, str]]:
        key = (blob_name, start_line, end_line)
        self.call_requests.append(key)
        if self.call_error is not None:
            raise self.call_error
        return list(self.calls.get(key, ()))


class FakeRelationStore:
    """Preset relation rows and recorded implementation queries."""

    def __init__(
        self,
        implementations: Sequence[RelatedOccurrence] = (),
        *,
        error: Exception | None = None,
    ) -> None:
        self.implementations = list(implementations)
        self.error = error
        self.implementation_requests: list[tuple[tuple[str, ...], int]] = []

    async def find_implementations(
        self,
        *,
        identifiers: Sequence[str],
        scope: SearchScope,
        limit: int = 8,
    ) -> list[RelatedOccurrence]:
        self.implementation_requests.append((tuple(identifiers), limit))
        if self.error is not None:
            raise self.error
        return [
            item
            for item in self.implementations
            if item.identifier in identifiers and item.hit.blob_name in scope.blob_names
        ][:limit]

    async def find_callers(
        self, *, identifiers: Sequence[str], scope: SearchScope, limit: int = 8
    ) -> list[RelatedOccurrence]:
        return []

    async def find_test_uses(
        self, *, identifiers: Sequence[str], scope: SearchScope, limit: int = 8
    ) -> list[RelatedOccurrence]:
        return []

    async def find_reexports(
        self, *, identifiers: Sequence[str], scope: SearchScope, limit: int = 4
    ) -> list[RelatedOccurrence]:
        return []

    async def defined_identifiers(
        self, occurrences: Sequence[tuple[str, str]], scope: SearchScope
    ) -> dict[tuple[str, str], tuple[str, ...]]:
        return {}


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


def retrieval_state(
    query: str,
    scope: SearchScope | None = None,
    *,
    audit: RetrievalAudit | None = None,
    intent: QueryIntent | None = None,
    strategy: RetrievalStrategy | None = None,
    lookup_identifiers: tuple[str, ...] | None = None,
    qualifiers: Mapping[str, tuple[str, ...]] | None = None,
    **fields: Any,
) -> RetrievalState:
    """A state routed from ``query`` the way the pipeline routes it.

    Keyword overrides pin the route under test: ``intent`` also selects its
    strategy unless ``strategy`` is given. ``fields`` are stage records and
    caches (``recall``, ``selected``, ``candidates``...).
    """
    route = route_query(query, path_index_available=False)
    changes: dict[str, Any] = {}
    if intent is not None:
        changes["intent"] = intent
        changes["strategy"] = get_strategy(intent)
    if strategy is not None:
        changes["strategy"] = strategy
    if lookup_identifiers is not None:
        changes["lookup_identifiers"] = lookup_identifiers
    if qualifiers is not None:
        changes["qualifiers"] = MappingProxyType(dict(qualifiers))
    return RetrievalState(
        query=query,
        scope=scope,
        route=replace(route, **changes),
        audit=audit,
        **fields,
    )
