"""Persisted index profiles prevent silent reuse across vector spaces."""

from dataclasses import replace

import pytest

from oce.application.index_lifecycle import IndexLifecycleManager, build_index_profile
from oce.shared.config.settings import (
    ChunkingSettings,
    MilvusSettings,
    RetrievalSettings,
    Settings,
)
from oce.shared.errors import ServiceNotReadyError
from oce.shared.index_profile import (
    EmbeddingIndexProfile,
    IndexProfile,
    StoredIndexProfile,
)


def _embedding(model: str = "embedding-v1") -> EmbeddingIndexProfile:
    return EmbeddingIndexProfile(
        enabled=True,
        endpoint_hash="a" * 64,
        model=model,
        dimensions=1024,
        query_instruction_hash="b" * 64,
        max_input_chars=8000,
        input_overlap_chars=400,
    )


class Store:
    def __init__(self, *, has_data: bool = False) -> None:
        self.stored: StoredIndexProfile | None = None
        self.has_data = has_data
        self.initializations = 0

    async def read(self):
        return self.stored

    async def has_index_data(self):
        return self.has_data

    async def initialize(self, profile):
        self.initializations += 1
        if self.stored is None:
            self.stored = StoredIndexProfile(
                profile.fingerprint,
                profile.canonical_json(),
            )
        return self.stored


class Probe:
    def __init__(self, has_data: bool) -> None:
        self.has_data = has_data

    async def has_index_data(self):
        return self.has_data


async def test_empty_index_initializes_once_and_accepts_same_profile():
    store = Store()
    manager = IndexLifecycleManager(store, Settings())

    first = await manager.ensure_compatible(_embedding())
    second = await manager.ensure_compatible(_embedding())

    assert first.fingerprint == second.fingerprint
    assert manager.current == second
    assert store.initializations == 1
    stats = await manager.index_profile_stats()
    assert stats.state == "compatible"
    assert stats.fingerprint == first.fingerprint
    assert len(stats.embedding_fingerprint or "") == 64
    assert stats.embedding_model == "embedding-v1"


async def test_embedding_or_chunking_change_is_rejected_without_overwrite():
    store = Store()
    await IndexLifecycleManager(store, Settings()).ensure_compatible(_embedding())
    original = store.stored

    with pytest.raises(ServiceNotReadyError, match="embedding.model"):
        await IndexLifecycleManager(store, Settings()).ensure_compatible(
            _embedding("embedding-v2")
        )

    changed_chunker = Settings(
        chunking=ChunkingSettings(semantic_enabled=False),
    )
    with pytest.raises(
        ServiceNotReadyError,
        match="semantic_chunking_enabled",
    ):
        await IndexLifecycleManager(store, changed_chunker).ensure_compatible(
            _embedding()
        )

    assert store.stored == original


async def test_source_admission_change_is_rejected_without_reusing_old_blobs():
    store = Store()
    profile = build_index_profile(Settings(), _embedding())
    old_profile = replace(profile, source_admission_version=0)
    store.stored = StoredIndexProfile(
        old_profile.fingerprint,
        old_profile.canonical_json(),
    )

    with pytest.raises(ServiceNotReadyError, match="source_admission_version"):
        await IndexLifecycleManager(store, Settings()).ensure_compatible(_embedding())


@pytest.mark.parametrize(
    ("settings", "changed_field"),
    [
        (
            Settings(milvus=MilvusSettings(endpoint="http://other-milvus:19530")),
            "vector_store_endpoint_hash",
        ),
        (
            Settings(milvus=MilvusSettings(collection_name="other_chunks")),
            "dense_collection_name",
        ),
        (
            Settings(milvus=MilvusSettings(dense_metric_type="IP")),
            "dense_metric_type",
        ),
        (
            Settings(retrieval=RetrievalSettings(path_index_enabled=False)),
            "path_index_enabled",
        ),
        (
            Settings(milvus=MilvusSettings(path_collection_name="other_paths")),
            "path_collection_name",
        ),
    ],
)
async def test_vector_store_identity_change_is_rejected(settings, changed_field):
    store = Store()
    await IndexLifecycleManager(store, Settings()).ensure_compatible(_embedding())

    with pytest.raises(ServiceNotReadyError, match=changed_field):
        await IndexLifecycleManager(store, settings).ensure_compatible(_embedding())


def test_vector_store_identity_normalizes_equivalent_settings(tmp_path):
    local_store = tmp_path / "vectors.db"
    first = build_index_profile(
        Settings(
            milvus=MilvusSettings(endpoint=str(local_store), path_collection_name="a"),
            retrieval=RetrievalSettings(path_index_enabled=False),
        ),
        _embedding(),
    )
    second = build_index_profile(
        Settings(
            milvus=MilvusSettings(
                endpoint=str(local_store.resolve()),
                path_collection_name="b",
            ),
            retrieval=RetrievalSettings(path_index_enabled=False),
        ),
        _embedding(),
    )

    assert first.fingerprint == second.fingerprint
    assert first.path_collection_name is None


async def test_legacy_index_without_profile_fails_closed():
    manager = IndexLifecycleManager(Store(has_data=True), Settings())

    with pytest.raises(ServiceNotReadyError, match="no lifecycle fingerprint"):
        await manager.ensure_compatible(_embedding())


async def test_vector_data_without_metadata_profile_fails_closed():
    manager = IndexLifecycleManager(
        Store(),
        Settings(),
        [Probe(False), Probe(True)],
    )

    with pytest.raises(ServiceNotReadyError, match="no lifecycle fingerprint"):
        await manager.ensure_compatible(_embedding())


async def test_stored_profile_is_reported_as_unverified_before_runtime_resolution():
    store = Store()
    initialized = IndexLifecycleManager(store, Settings())
    profile = await initialized.ensure_compatible(_embedding())

    stats = await IndexLifecycleManager(store, Settings()).index_profile_stats()

    assert stats.state == "stored_unverified"
    assert stats.fingerprint == profile.fingerprint
    assert stats.embedding_dimensions == 1024


async def test_unparsable_stored_profile_reports_fingerprint_only():
    store = Store()
    await IndexLifecycleManager(store, Settings()).ensure_compatible(_embedding())
    assert store.stored is not None
    store.stored = StoredIndexProfile(store.stored.fingerprint, '{"schema_version": 1}')

    stats = await IndexLifecycleManager(store, Settings()).index_profile_stats()

    assert stats.state == "stored_unverified"
    assert stats.fingerprint == store.stored.fingerprint
    assert stats.schema_version is None
    assert stats.embedding_model is None


def test_index_profile_round_trips_through_json():
    profile = build_index_profile(Settings(), _embedding())

    restored = IndexProfile.from_json(profile.canonical_json())

    assert restored == profile
    assert IndexProfile.from_json("not json") is None
    assert IndexProfile.from_json("[]") is None


async def test_corrupted_stored_profile_fails_closed():
    store = Store()
    initialized = IndexLifecycleManager(store, Settings())
    await initialized.ensure_compatible(_embedding())
    assert store.stored is not None
    store.stored = StoredIndexProfile(store.stored.fingerprint, "{}")

    with pytest.raises(ServiceNotReadyError, match="integrity check"):
        await IndexLifecycleManager(store, Settings()).ensure_compatible(_embedding())
