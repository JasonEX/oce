"""Path-only recall resolves one ready source chunk per file."""

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from oce.infrastructure.persistence.models import (
    BlobChunkModel,
    BlobModel,
    ChunkModel,
)
from oce.infrastructure.persistence.path_content_store import SqlPathContentStore
from oce.shared.database.session import Base


async def test_representative_chunks_are_batched_ordered_and_ready_only():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    ready_name = "a" * 64
    pending_name = "b" * 64
    first_hash = "c" * 64
    second_hash = "d" * 64
    pending_hash = "e" * 64

    async with sessions() as session:
        session.add_all(
            [
                BlobModel(
                    blob_name=ready_name,
                    path="src/ready.py",
                    content_size=20,
                    file_type="text",
                    status="ready",
                ),
                BlobModel(
                    blob_name=pending_name,
                    path="src/pending.py",
                    content_size=10,
                    file_type="text",
                    status="pending",
                ),
                ChunkModel(
                    content_hash=first_hash,
                    content="first chunk",
                    content_size=11,
                    embedded=True,
                ),
                ChunkModel(
                    content_hash=second_hash,
                    content="second chunk",
                    content_size=12,
                    embedded=True,
                ),
                ChunkModel(
                    content_hash=pending_hash,
                    content="pending chunk",
                    content_size=13,
                    embedded=False,
                ),
                BlobChunkModel(
                    blob_name=ready_name,
                    content_hash=second_hash,
                    start_line=20,
                    end_line=21,
                    chunk_index=1,
                ),
                BlobChunkModel(
                    blob_name=ready_name,
                    content_hash=first_hash,
                    start_line=1,
                    end_line=2,
                    chunk_index=0,
                ),
                BlobChunkModel(
                    blob_name=pending_name,
                    content_hash=pending_hash,
                    start_line=1,
                    end_line=1,
                    chunk_index=0,
                ),
            ]
        )
        await session.commit()

    hits = await SqlPathContentStore(sessions).get_representative_chunks(
        [pending_name, ready_name, ready_name, "f" * 64]
    )

    assert len(hits) == 1
    assert hits[0].blob_name == ready_name
    assert hits[0].path == "src/ready.py"
    assert hits[0].content_hash == first_hash
    assert hits[0].content == "first chunk"
    assert (hits[0].start_line, hits[0].end_line) == (1, 2)
    await engine.dispose()
