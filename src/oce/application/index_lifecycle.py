"""Fail-closed compatibility checks for persisted retrieval artifacts."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from oce.shared.config.settings import Settings
from oce.shared.errors import ServiceNotReadyError
from oce.shared.index_profile import (
    CHUNKER_VERSION,
    INDEX_SCHEMA_VERSION,
    LEXICAL_INDEX_VERSION,
    PATH_DOCUMENT_VERSION,
    SOURCE_ADMISSION_VERSION,
    SYMBOL_EXTRACTION_VERSION,
    EmbeddingIndexProfile,
    IndexDataProbe,
    IndexProfile,
    IndexProfileStore,
    StoredIndexProfile,
    profile_value_hash,
)
from oce.shared.index_stats import IndexProfileStats


def build_index_profile(
    settings: Settings,
    embedding: EmbeddingIndexProfile,
) -> IndexProfile:
    chunking = settings.chunking
    milvus = settings.milvus
    endpoint = milvus.endpoint.strip()
    if "://" in endpoint:
        endpoint = endpoint.rstrip("/")
    else:
        endpoint = str(Path(endpoint).expanduser().resolve())
    return IndexProfile(
        schema_version=INDEX_SCHEMA_VERSION,
        vector_store_endpoint_hash=profile_value_hash(endpoint),
        dense_collection_name=milvus.collection_name,
        dense_metric_type=milvus.dense_metric_type.upper(),
        path_index_enabled=settings.retrieval.path_index_enabled,
        path_collection_name=(
            milvus.path_collection_name
            if settings.retrieval.path_index_enabled
            else None
        ),
        chunker_version=CHUNKER_VERSION,
        semantic_chunking_enabled=chunking.semantic_enabled,
        semantic_max_chunk_chars=chunking.semantic_max_chunk_chars,
        recursive_chunk_size=chunking.recursive_chunk_size,
        recursive_chunk_overlap=chunking.recursive_chunk_overlap,
        symbol_extraction_version=SYMBOL_EXTRACTION_VERSION,
        path_document_version=PATH_DOCUMENT_VERSION,
        source_admission_version=SOURCE_ADMISSION_VERSION,
        embedding=embedding,
        lexical_index_enabled=settings.retrieval.lexical_enabled,
        lexical_index_version=(
            LEXICAL_INDEX_VERSION if settings.retrieval.lexical_enabled else 0
        ),
    )


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if not isinstance(value, dict):
        return {prefix: value}
    flattened: dict[str, Any] = {}
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        flattened.update(_flatten(item, path))
    return flattened


def _changed_fields(stored_json: str, current_json: str) -> tuple[str, ...]:
    try:
        stored = _flatten(json.loads(stored_json))
        current = _flatten(json.loads(current_json))
    except (TypeError, ValueError, json.JSONDecodeError):
        return ("stored_profile",)
    return tuple(
        key
        for key in sorted(stored.keys() | current.keys())
        if stored.get(key) != current.get(key)
    )


class IndexLifecycleManager:
    """Persist the first profile and reject every incompatible reuse thereafter."""

    def __init__(
        self,
        store: IndexProfileStore,
        settings: Settings,
        artifact_probes: Sequence[IndexDataProbe] = (),
    ) -> None:
        self._store = store
        self._settings = settings
        self._artifact_probes = tuple(artifact_probes)
        self._lock = asyncio.Lock()
        self._current: IndexProfile | None = None

    @property
    def current(self) -> IndexProfile | None:
        return self._current

    async def ensure_compatible(
        self,
        embedding: EmbeddingIndexProfile,
    ) -> IndexProfile:
        current = build_index_profile(self._settings, embedding)
        async with self._lock:
            stored = await self._store.read()
            if stored is None:
                has_index_data = await self._store.has_index_data()
                if not has_index_data:
                    for probe in self._artifact_probes:
                        if await probe.has_index_data():
                            has_index_data = True
                            break
                if has_index_data:
                    raise ServiceNotReadyError(
                        "Existing index has no lifecycle fingerprint. Use a new data "
                        "directory, or clean metadata and vector storage, then fully "
                        "resync clients; existing data was left untouched."
                    )
                stored = await self._store.initialize(current)
            self._assert_match(stored, current)
            self._current = current
            return current

    async def index_profile_stats(self) -> IndexProfileStats:
        if self._current is not None:
            return self._stats("compatible", self._current.fingerprint, self._current)
        stored = await self._store.read()
        if stored is None:
            return IndexProfileStats("uninitialized")
        # 存量 JSON 可能来自更旧的 schema；解析不出来时只报告指纹。
        return self._stats(
            "stored_unverified",
            stored.fingerprint,
            IndexProfile.from_json(stored.profile_json),
        )

    @staticmethod
    def _stats(
        state: str,
        fingerprint: str,
        profile: IndexProfile | None,
    ) -> IndexProfileStats:
        if profile is None:
            return IndexProfileStats(state=state, fingerprint=fingerprint)
        embedding = profile.embedding
        return IndexProfileStats(
            state=state,
            fingerprint=fingerprint,
            schema_version=profile.schema_version,
            embedding_enabled=embedding.enabled,
            embedding_fingerprint=embedding.fingerprint,
            embedding_model=embedding.model,
            embedding_dimensions=embedding.dimensions,
        )

    @staticmethod
    def _assert_match(
        stored: StoredIndexProfile,
        current: IndexProfile,
    ) -> None:
        if profile_value_hash(stored.profile_json) != stored.fingerprint:
            raise ServiceNotReadyError(
                "Stored index profile failed its integrity check. Existing data was "
                "left untouched; inspect the metadata store before rebuilding."
            )
        if stored.fingerprint == current.fingerprint:
            return
        changed = _changed_fields(stored.profile_json, current.canonical_json())
        changed_text = ", ".join(changed) if changed else "unknown"
        raise ServiceNotReadyError(
            "Index profile mismatch "
            f"(stored={stored.fingerprint[:12]}, current={current.fingerprint[:12]}; "
            f"changed={changed_text}). Use a new data directory, or clean metadata "
            "and vector storage, then fully resync clients; existing data was left "
            "untouched."
        )
