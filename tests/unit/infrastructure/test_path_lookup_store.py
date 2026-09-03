"""Exact path suffix and basename lookup inside a scope."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from oce.domain.blob.blob import Blob, BlobStatus
from oce.domain.services.search import SearchScope
from oce.infrastructure.persistence.path_lookup_store import SqlPathLookupStore
from oce.infrastructure.persistence.sql_blob_repo import SqlBlobRepository
from oce.shared.database.session import Base
from tests.conftest import make_sha256


@pytest.fixture
async def sessions():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def test_suffix_beats_basename_and_scope_applies(sessions):
    paths = {
        "core": "xarray/core/dataset.py",
        "backend": "xarray/backends/dataset.py",
        "config": "config.json",
        "pending": "xarray/core/pending.py",
    }
    names = {key: make_sha256(key) for key in paths}
    async with sessions() as session:
        repo = SqlBlobRepository(session)
        for key, path in paths.items():
            await repo.save(
                Blob(
                    names[key],
                    path,
                    BlobStatus.PENDING if key == "pending" else BlobStatus.READY,
                )
            )
        await session.commit()
    store = SqlPathLookupStore(sessions)

    scores = await store.match_paths(
        filenames=("dataset.py", "config.json", "pending.py"),
        paths=("home/u/xarray/xarray/core/dataset.py",),
        scope=SearchScope(frozenset(names.values())),
    )
    assert scores[names["core"]] == 1.0
    assert scores[names["backend"]] == 0.9
    assert scores[names["config"]] == 0.9
    assert names["pending"] not in scores

    scoped = await store.match_paths(
        filenames=("dataset.py",),
        paths=(),
        scope=SearchScope(frozenset({names["backend"]})),
    )
    assert set(scoped) == {names["backend"]}

    assert (
        await store.match_paths(
            filenames=(), paths=(), scope=SearchScope(frozenset(names.values()))
        )
        == {}
    )


async def test_full_suffix_is_not_truncated_by_many_basename_matches(sessions):
    target_name = make_sha256("target")
    decoys = {
        make_sha256(f"decoy-{index}"): f"pkg{index:03d}/dataset.py"
        for index in range(100)
    }
    async with sessions() as session:
        repo = SqlBlobRepository(session)
        await repo.save_many(
            [
                *(Blob(name, path, BlobStatus.READY) for name, path in decoys.items()),
                Blob(target_name, "xarray/core/dataset.py", BlobStatus.READY),
            ]
        )
        await session.commit()

    scores = await SqlPathLookupStore(sessions).match_paths(
        filenames=("dataset.py",),
        paths=("home/u/xarray/core/dataset.py",),
        scope=SearchScope(frozenset({target_name, *decoys})),
        limit=20,
    )

    assert scores[target_name] == 1.0
