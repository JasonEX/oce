"""SQL persistence for the singleton active index profile."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from oce.infrastructure.persistence.models import (
    BlobModel,
    ChainMemberModel,
    ChainModel,
    ChunkModel,
    IndexProfileModel,
    SymbolOccurrenceModel,
)
from oce.shared.index_profile import IndexProfile, StoredIndexProfile


_ACTIVE_PROFILE_KEY = "active"


class SqlIndexProfileStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def read(self) -> StoredIndexProfile | None:
        async with self._session_factory() as session:
            row = await session.get(IndexProfileModel, _ACTIVE_PROFILE_KEY)
            return self._to_stored(row)

    async def has_index_data(self) -> bool:
        queries = (
            select(BlobModel.blob_name).limit(1),
            select(ChunkModel.content_hash).limit(1),
            select(ChainModel.chain_id).limit(1),
            select(ChainMemberModel.chain_id).limit(1),
            select(SymbolOccurrenceModel.id).limit(1),
        )
        async with self._session_factory() as session:
            for query in queries:
                if (await session.execute(query)).first() is not None:
                    return True
        return False

    async def initialize(self, profile: IndexProfile) -> StoredIndexProfile:
        values = {
            "profile_key": _ACTIVE_PROFILE_KEY,
            "fingerprint": profile.fingerprint,
            "profile_json": profile.canonical_json(),
        }
        async with self._session_factory() as session:
            dialect = session.get_bind().dialect.name
            insert = sqlite_insert if dialect == "sqlite" else pg_insert
            statement = insert(IndexProfileModel).values(values)
            statement = statement.on_conflict_do_nothing(index_elements=["profile_key"])
            await session.execute(statement)
            await session.commit()
            row = await session.get(IndexProfileModel, _ACTIVE_PROFILE_KEY)
            if row is None:
                raise RuntimeError("index profile initialization did not persist a row")
            stored = self._to_stored(row)
            assert stored is not None
            return stored

    @staticmethod
    def _to_stored(row: IndexProfileModel | None) -> StoredIndexProfile | None:
        if row is None:
            return None
        return StoredIndexProfile(
            fingerprint=row.fingerprint,
            profile_json=row.profile_json,
        )
