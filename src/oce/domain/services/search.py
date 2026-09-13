"""Retrieval value objects and store protocols.

``SearchHit`` is the immutable hit; the store protocols are implemented by
the infrastructure layer and are all the domain depends on.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

# A hit's role in the result: primary answers the request; the rest are
# excerpts appended by relation lane: related is a referenced definition,
# caller a call site, implementation a subclass or impl, test a covering
# test, reexport a barrel entry, chain a definition on the call path between
# two endpoints (numbered by hop).
HitRole = Literal[
    "primary", "related", "caller", "implementation", "test", "reexport", "chain"
]


@dataclass(frozen=True)
class SearchHit:
    """One retrieved chunk."""

    blob_name: str
    path: str
    content: str
    score: float
    content_hash: str = ""
    start_line: int = 1
    end_line: int = 1
    # Enclosing scope chain recorded at chunking (``class Foo > def bar``);
    # None without an AST.
    context: str | None = None
    role: HitRole = "primary"
    hop: int | None = None


@dataclass(frozen=True)
class SearchScope:
    """One resolved workspace scope shared by every retrieval backend.

    ``blob_names`` is the authoritative materialized scope used by Milvus.  When
    the scope came from a checkpoint, the chain metadata lets SQL stores apply
    the same scope as a relation instead of expanding every member into an
    ``IN`` clause.  Request deltas remain explicit because they have not been
    committed to the checkpoint yet.
    """

    blob_names: frozenset[str]
    chain_id: str | None = None
    chain_version: int | None = None
    added_blob_names: frozenset[str] = frozenset()
    deleted_blob_names: frozenset[str] = frozenset()


# One source occurrence: (blob_name, path, start_line, end_line, content identity).
SearchHitKey = tuple[str, str, int, int, str]


def search_hit_key(hit: SearchHit) -> SearchHitKey:
    """Identify one source occurrence, including legacy hits without a hash."""
    return (
        hit.blob_name,
        hit.path,
        hit.start_line,
        hit.end_line,
        hit.content_hash or hit.content,
    )


class SearchStore(Protocol):
    """Dense vector search (Milvus 3.0)."""

    async def search(
        self,
        *,
        query_vector: list[float],
        allowed_blob_names: Sequence[str] | None = None,
        top_k: int = 50,
        vector_threshold: float = 0.0,
    ) -> list[SearchHit]:
        """Hits by descending similarity, filtered to ``allowed_blob_names`` when given."""
        ...


@dataclass(frozen=True)
class DefinitionHit:
    """One symbol definition inside an indexed chunk.

    ``hit`` spans the whole chunk; ``start_line``/``end_line`` are the
    definition's own lines so callers can cut a signature-sized excerpt.
    """

    identifier: str
    kind: str
    hit: SearchHit
    start_line: int
    end_line: int
    # Innermost definition holding the declaration (``Router`` for
    # ``impl Router { fn route }``); empty at module level.
    enclosing: str = ""


@dataclass(frozen=True)
class HubDefinition:
    """A declared name the request's words spell, with how widely it is used.

    ``referencing_files`` counts the scoped files that call, import or
    extend the name; ``names_package`` says the name is also a directory of
    the scope (``_pytest``, ``routing``), in which case the references are
    to the package rather than to this declaration.
    """

    identifier: str
    definitions: tuple[DefinitionHit, ...]
    referencing_files: int
    names_package: bool


class ExactSearchStore(Protocol):
    """Exact recall of indexed chunks by code identifier."""

    async def search_exact(
        self,
        *,
        identifiers: Sequence[str],
        scope: SearchScope,
        top_k: int = 50,
        kinds: Sequence[str] | None = None,
    ) -> list[SearchHit]:
        """``kinds`` restricts the occurrence kinds; None allows every kind."""
        ...

    async def find_definitions(
        self,
        *,
        identifiers: Sequence[str],
        scope: SearchScope,
        max_per_identifier: int = 3,
        enclosing: Sequence[str] | None = None,
    ) -> list[DefinitionHit]:
        """Definitions/endpoints of the identifiers whose scope-wide count fits the cap.

        ``enclosing`` restricts the rows to declarations recorded inside one
        of the named definitions (``Router`` for ``Router::route``), so the
        cap measures the pinned name rather than every ``route`` in scope.
        """
        ...

    async def occurrence_kinds(
        self,
        occurrences: Sequence[tuple[str, str]],
        scope: SearchScope,
    ) -> dict[tuple[str, str], frozenset[str]]:
        """Symbol occurrence kinds for scoped ``(blob_name, content_hash)`` pairs."""
        ...

    async def calls_within(
        self,
        *,
        blob_name: str,
        start_line: int,
        end_line: int,
        scope: SearchScope,
    ) -> list[tuple[str, int, str]]:
        """``(identifier, line, enclosing)`` of the calls inside one span, in line order."""
        ...

    async def chunk_for_line(
        self,
        *,
        blob_name: str,
        line: int,
        scope: SearchScope,
    ) -> SearchHit | None:
        """The indexed chunk of ``blob_name`` whose line span contains ``line``."""
        ...

    async def find_hub_definitions(
        self,
        *,
        spellings: Sequence[str],
        scope: SearchScope,
        max_per_identifier: int = 6,
    ) -> list[HubDefinition]:
        """Declared names among ``spellings`` with their reference fan-in."""
        ...


class LexicalSearchStore(Protocol):
    """Term and phrase recall over the chunk token index, ranked lexically.

    ``required`` is a group of tokens of which at least one must match:
    reference requests use it to tell chunks that use the identifier from
    chunks that merely share a sub-word; ranking still uses every term.
    """

    async def search_lexical(
        self,
        *,
        terms: Sequence[str],
        phrases: Sequence[str],
        scope: SearchScope,
        top_k: int = 30,
        required: Sequence[str] = (),
    ) -> list[SearchHit]: ...


class PathLookupStore(Protocol):
    """Exact path/basename lookup inside the scope; no embedding involved."""

    async def match_paths(
        self,
        *,
        filenames: Sequence[str],
        paths: Sequence[str],
        scope: SearchScope,
        limit: int = 20,
    ) -> dict[str, float]:
        """Return ``blob_name -> score`` (1.0 full-path suffix, 0.9 basename)."""
        ...


@dataclass(frozen=True)
class VectorRecord:
    """One chunk occurrence with its vector, ready for the vector index."""

    chunk_id: str
    content_hash: str
    blob_name: str
    path: str
    content: str
    start_line: int
    end_line: int
    vector: list[float]
    context: str | None = None


class VectorIndex(Protocol):
    """The write side of the vector index."""

    async def upsert(self, records: Sequence[VectorRecord]) -> None: ...

    async def delete(self, blob_names: Sequence[str]) -> None: ...
