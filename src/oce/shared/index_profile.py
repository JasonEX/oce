"""Stable, secret-free identity for persisted retrieval artifacts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Protocol

INDEX_SCHEMA_VERSION = 2
CHUNKER_VERSION = 3
EMBEDDING_PIPELINE_VERSION = 1
SYMBOL_EXTRACTION_VERSION = 1
PATH_DOCUMENT_VERSION = 1
SOURCE_ADMISSION_VERSION = 1


def profile_value_hash(value: str) -> str:
    """Hash configuration text that may contain prompts or internal endpoints."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EmbeddingIndexProfile:
    enabled: bool
    endpoint_hash: str | None = None
    model: str | None = None
    dimensions: int | None = None
    query_instruction_hash: str | None = None
    max_input_chars: int | None = None
    input_overlap_chars: int | None = None
    pipeline_version: int = EMBEDDING_PIPELINE_VERSION

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
class IndexProfile:
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
class StoredIndexProfile:
    fingerprint: str
    profile_json: str


class IndexDataProbe(Protocol):
    async def has_index_data(self) -> bool: ...


class IndexProfileStore(IndexDataProbe, Protocol):
    async def read(self) -> StoredIndexProfile | None: ...

    async def initialize(self, profile: IndexProfile) -> StoredIndexProfile: ...
