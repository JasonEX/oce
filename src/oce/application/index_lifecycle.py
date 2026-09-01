"""Fail-closed compatibility checks for persisted retrieval artifacts."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from oce.shared.config.settings import Settings
from oce.shared.errors import ServiceNotReadyError
from oce.shared.index_profile import (
    CHUNKER_VERSION,
    INDEX_SCHEMA_VERSION,
    PATH_DOCUMENT_VERSION,
    SYMBOL_EXTRACTION_VERSION,
    EmbeddingIndexProfile,
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
    return IndexProfile(
        schema_version=INDEX_SCHEMA_VERSION,
        chunker_version=CHUNKER_VERSION,
        semantic_chunking_enabled=chunking.semantic_enabled,
        semantic_max_chunk_chars=chunking.semantic_max_chunk_chars,
        recursive_chunk_size=chunking.recursive_chunk_size,
        recursive_chunk_overlap=chunking.recursive_chunk_overlap,
        symbol_extraction_version=SYMBOL_EXTRACTION_VERSION,
        path_document_version=PATH_DOCUMENT_VERSION,
        embedding=embedding,
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

    def __init__(self, store: IndexProfileStore, settings: Settings) -> None:
        self._store = store
        self._settings = settings
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
                if await self._store.has_index_data():
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
            return self._stats_from_payload(
                "compatible",
                self._current.fingerprint,
                json.loads(self._current.canonical_json()),
            )
        stored = await self._store.read()
        if stored is None:
            return IndexProfileStats("uninitialized")
        try:
            payload = json.loads(stored.profile_json)
        except (TypeError, ValueError):
            payload = {}
        return self._stats_from_payload(
            "stored_unverified",
            stored.fingerprint,
            payload,
        )

    @staticmethod
    def _stats_from_payload(
        state: str,
        fingerprint: str,
        payload: Any,
    ) -> IndexProfileStats:
        profile = payload if isinstance(payload, dict) else {}
        embedding = profile.get("embedding")
        if not isinstance(embedding, dict):
            embedding = {}
        schema_version = profile.get("schema_version")
        embedding_enabled = embedding.get("enabled")
        embedding_model = embedding.get("model")
        embedding_dimensions = embedding.get("dimensions")
        embedding_fingerprint = (
            profile_value_hash(
                json.dumps(
                    embedding,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            if embedding
            else None
        )
        return IndexProfileStats(
            state=state,
            fingerprint=fingerprint,
            schema_version=(
                schema_version
                if isinstance(schema_version, int)
                and not isinstance(schema_version, bool)
                else None
            ),
            embedding_enabled=(
                embedding_enabled if isinstance(embedding_enabled, bool) else None
            ),
            embedding_fingerprint=embedding_fingerprint,
            embedding_model=(
                embedding_model if isinstance(embedding_model, str) else None
            ),
            embedding_dimensions=(
                embedding_dimensions
                if isinstance(embedding_dimensions, int)
                and not isinstance(embedding_dimensions, bool)
                else None
            ),
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
