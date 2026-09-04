"""Stable, secret-free identity for persisted retrieval artifacts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Protocol

# 3: blob_chunks.context + chunk_lexical term index.
INDEX_SCHEMA_VERSION = 3
# 4: cAST chunks carry the enclosing scope chain.
CHUNKER_VERSION = 4
# 2: embedding input = File + Context header + code.
EMBEDDING_PIPELINE_VERSION = 2
# 2: tree-sitter definitions/imports with real spans; regex fallback + endpoints.
SYMBOL_EXTRACTION_VERSION = 3
PATH_DOCUMENT_VERSION = 1
SOURCE_ADMISSION_VERSION = 1
LEXICAL_INDEX_VERSION = 1


def profile_value_hash(value: str) -> str:
    """Hash configuration text that may contain prompts or internal endpoints."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class _CanonicalProfile:
    """Deterministic JSON identity for a frozen profile dataclass."""

    def canonical_json(self) -> str:
        return json.dumps(
            asdict(self),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @property
    def fingerprint(self) -> str:
        return profile_value_hash(self.canonical_json())


@dataclass(frozen=True)
class EmbeddingIndexProfile(_CanonicalProfile):
    enabled: bool
    endpoint_hash: str | None = None
    model: str | None = None
    dimensions: int | None = None
    query_instruction_hash: str | None = None
    max_input_chars: int | None = None
    input_overlap_chars: int | None = None
    pipeline_version: int = EMBEDDING_PIPELINE_VERSION


@dataclass(frozen=True)
class IndexProfile(_CanonicalProfile):
    schema_version: int
    vector_store_endpoint_hash: str
    dense_collection_name: str
    dense_metric_type: str
    path_index_enabled: bool
    path_collection_name: str | None
    chunker_version: int
    semantic_chunking_enabled: bool
    semantic_max_chunk_chars: int
    recursive_chunk_size: int
    recursive_chunk_overlap: int
    symbol_extraction_version: int
    path_document_version: int
    source_admission_version: int
    embedding: EmbeddingIndexProfile
    lexical_index_enabled: bool
    lexical_index_version: int

    @classmethod
    def from_json(cls, text: str) -> IndexProfile | None:
        """Rebuild a stored profile; ``None`` when the payload no longer fits."""
        try:
            payload = json.loads(text)
            embedding = EmbeddingIndexProfile(**payload.pop("embedding"))
            return cls(embedding=embedding, **payload)
        except (TypeError, ValueError, KeyError, AttributeError):
            return None


@dataclass(frozen=True)
class StoredIndexProfile:
    fingerprint: str
    profile_json: str


class IndexDataProbe(Protocol):
    async def has_index_data(self) -> bool: ...


class IndexProfileStore(IndexDataProbe, Protocol):
    async def read(self) -> StoredIndexProfile | None: ...

    async def initialize(self, profile: IndexProfile) -> StoredIndexProfile: ...
