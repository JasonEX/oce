"""Metadata index counters reflect persisted operational state."""

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from oce.infrastructure.persistence.index_stats_reader import (
    SqlMetadataIndexStatsReader,
)
from oce.infrastructure.persistence.models import (
    BlobChunkModel,
    BlobModel,
    BlobStagingModel,
    ChainMemberModel,
    ChainModel,
    ChunkModel,
    SymbolOccurrenceModel,
)
from oce.shared.database.session import Base


async def test_metadata_index_stats_count_status_and_index_tables():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(timezone.utc)
    blob_names = [str(index) * 64 for index in range(1, 4)]
    chunk_names = ["a" * 64, "b" * 64]

    async with sessions() as session:
        session.add_all(
            [
                BlobModel(
                    blob_name=name,
                    path=f"src/{index}.py",
                    content_size=10,
                    file_type="text",
                    status=status,
                    last_seen=now,
                    created_at=now,
                )
                for index, (name, status) in enumerate(
                    zip(blob_names, ("ready", "pending", "error"), strict=True)
                )
            ]
        )
        session.add_all(
            [
                ChunkModel(
                    content_hash=chunk_names[0],
                    content="def target(): pass",
                    content_size=18,
                    embedded=True,
                ),
                ChunkModel(
                    content_hash=chunk_names[1],
                    content="pending",
                    content_size=7,
                    embedded=False,
                ),
                BlobChunkModel(
                    blob_name=blob_names[0],
                    content_hash=chunk_names[0],
                    start_line=1,
                    end_line=1,
                    chunk_index=0,
                ),
                SymbolOccurrenceModel(
                    identifier="target",
                    blob_name=blob_names[0],
                    content_hash=chunk_names[0],
                    kind="definition",
                    start_line=1,
                    end_line=1,
                ),
                BlobStagingModel(blob_name=blob_names[1], content="pending"),
                ChainModel(chain_id="c" * 64, version=1, total_blobs=1),
                ChainMemberModel(chain_id="c" * 64, blob_name=blob_names[0]),
            ]
        )
        await session.commit()

    stats = await SqlMetadataIndexStatsReader(sessions).read()

    assert stats.blobs_total == 3
    assert (stats.blobs_ready, stats.blobs_pending, stats.blobs_error) == (1, 1, 1)
    assert stats.chunks_total == 2
    assert stats.chunks_embedded == 1
    assert stats.blob_chunk_links == 1
    assert stats.symbol_occurrences == 1
    assert stats.chains == 1
    assert stats.chain_members == 1
    assert stats.staging_blobs == 1
    await engine.dispose()
