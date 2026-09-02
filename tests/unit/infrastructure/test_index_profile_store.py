"""SQL index-profile store keeps one immutable active identity."""

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from oce.application.index_lifecycle import build_index_profile
from oce.infrastructure.persistence.index_profile_store import SqlIndexProfileStore
from oce.infrastructure.persistence.models import BlobModel
from oce.shared.config.settings import Settings
from oce.shared.database.session import Base
from oce.shared.index_profile import EmbeddingIndexProfile


async def _store():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    return engine, sessions, SqlIndexProfileStore(sessions)


def _profile(model: str):
    return build_index_profile(
        Settings(),
        EmbeddingIndexProfile(
            enabled=True,
            endpoint_hash="a" * 64,
            model=model,
            dimensions=1024,
            query_instruction_hash="b" * 64,
            max_input_chars=8000,
            input_overlap_chars=400,
        ),
    )


async def test_initialize_is_first_writer_wins():
    engine, _sessions, store = await _store()

    first = await store.initialize(_profile("first"))
    second = await store.initialize(_profile("second"))

    assert second == first
    assert await store.read() == first
    assert await store.has_index_data() is False
    await engine.dispose()


async def test_has_index_data_detects_legacy_blob_rows():
    engine, sessions, store = await _store()
    async with sessions() as session:
        session.add(
            BlobModel(
                blob_name="a" * 64,
                path="src/main.py",
                content_size=1,
                file_type="text",
                status="ready",
            )
        )
        await session.commit()

    assert await store.has_index_data() is True
    await engine.dispose()
