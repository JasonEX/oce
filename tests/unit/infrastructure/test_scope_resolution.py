"""A resolved ready scope stays identical across dense and SQL recall."""

from __future__ import annotations

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from oce.application.queries.status import ResolveScopeQuery, ResolveScopeQueryHandler
from oce.domain.blob.blob import Blob, BlobStatus
from oce.domain.chain.chain import Chain
from oce.domain.chunk import Chunk
from oce.infrastructure.persistence.models import BlobModel
from oce.infrastructure.persistence.sql_blob_repo import SqlBlobRepository
from oce.infrastructure.persistence.sql_chain_repo import SqlChainRepository
from oce.infrastructure.persistence.sql_chunk_repo import SqlChunkRepository
from oce.infrastructure.persistence.sql_symbol_projection import SqlSymbolProjection
from oce.infrastructure.persistence.symbol_search_store import SymbolSearchStore
from oce.infrastructure.persistence.uow import SqlAlchemyUnitOfWork
from oce.infrastructure.regex_symbol_provider import RegexSymbolProvider
from oce.shared.database.session import Base
from tests.conftest import make_sha256


@pytest.mark.parametrize("late_in_checkpoint", [False, True])
async def test_nonready_blob_that_finishes_after_resolution_stays_outside_sql_scope(
    late_in_checkpoint: bool,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )
        provider = RegexSymbolProvider()
        names = {name: make_sha256(name) for name in ("early_symbol", "late_symbol")}
        async with sessions() as session:
            for name, blob_name in names.items():
                path = f"src/{name}.py"
                content = f"def {name}(): pass"
                chunk = Chunk(Chunk.compute_hash(content), path, content, 1, 1)
                blob = Blob(blob_name, path, BlobStatus.READY, chunks=[chunk.to_ref()])
                await SqlChunkRepository(session).save_many([chunk])
                await SqlBlobRepository(session).save(blob)
                await SqlSymbolProjection(session, provider).index(
                    blob, [chunk], content
                )
            members = (
                tuple(names.values())
                if late_in_checkpoint
                else (names["early_symbol"],)
            )
            chain = await SqlChainRepository(session).create(members)
            await session.execute(
                update(BlobModel)
                .where(BlobModel.blob_name == names["late_symbol"])
                .values(status=BlobStatus.PENDING.value)
            )
            await session.commit()
        handler = ResolveScopeQueryHandler(
            lambda: SqlAlchemyUnitOfWork(sessions, provider)
        )
        resolved = await handler.handle(
            ResolveScopeQuery(
                checkpoint_id=Chain.format_checkpoint_token(
                    chain.chain_id, chain.version
                ),
                added_blobs=() if late_in_checkpoint else (names["late_symbol"],),
            )
        )
        scope = resolved.scope
        assert scope.blob_names == frozenset({names["early_symbol"]})
        assert scope.deleted_blob_names == frozenset({names["late_symbol"]})
        async with sessions() as session:
            await session.execute(
                update(BlobModel)
                .where(BlobModel.blob_name == names["late_symbol"])
                .values(status=BlobStatus.READY.value)
            )
            await session.commit()

        store = SymbolSearchStore(sessions)
        assert await store.search_exact(identifiers=("late_symbol",), scope=scope) == []
        assert (
            await store.definition_counts(identifiers=("late_symbol",), scope=scope)
            == {}
        )
        early = await store.search_exact(identifiers=("early_symbol",), scope=scope)
        assert [hit.blob_name for hit in early] == [names["early_symbol"]]
    finally:
        await engine.dispose()
