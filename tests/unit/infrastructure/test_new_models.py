"""The ORM tables create on SQLite and PostgreSQL alike (no PG-only column types)."""

import pytest
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.mark.asyncio
async def test_new_models_create_in_sqlite():
    """The tables can be created in SQLite."""
    from sqlalchemy.orm import declarative_base

    from oce.infrastructure.persistence.models import (
        BlobChunkModel,
        BlobModel,
        ChainMemberModel,
        ChainModel,
        ChunkModel,
    )

    # A temporary Base keeps the global metadata untouched.
    Base = declarative_base()

    # copy the table definitions
    class TestBlobModel(Base):
        __table__ = BlobModel.__table__.to_metadata(Base.metadata)

    class TestChunkModel(Base):
        __table__ = ChunkModel.__table__.to_metadata(Base.metadata)

    class TestBlobChunkModel(Base):
        __table__ = BlobChunkModel.__table__.to_metadata(Base.metadata)

    class TestChainModel(Base):
        __table__ = ChainModel.__table__.to_metadata(Base.metadata)

    class TestChainMemberModel(Base):
        __table__ = ChainMemberModel.__table__.to_metadata(Base.metadata)

    # create the tables in an in-memory SQLite database
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=True)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # the tables exist (a simple query works)
    from sqlalchemy import text

    async with engine.connect() as conn:
        # all five tables exist
        result = await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'")
        )
        tables = {row[0] for row in result}

        assert "blobs" in tables
        assert "chunks" in tables
        assert "blob_chunks" in tables
        assert "chains" in tables
        assert "chain_members" in tables

    await engine.dispose()


@pytest.mark.asyncio
async def test_new_models_create_in_postgresql():
    """The tables can be created in PostgreSQL when one is configured."""
    import uuid

    from dotenv import dotenv_values

    # Read .env directly, bypassing pytest's environment overrides.
    env_config = dotenv_values(".env")
    db_url = env_config.get("DB_URL")

    if not db_url or "postgresql" not in str(db_url):
        pytest.skip(f"No PostgreSQL configured in .env (got: {db_url})")

    from sqlalchemy import text

    # Importing the models registers the tables on Base.metadata.
    from oce.infrastructure.persistence import models
    from oce.shared.database.session import Base

    assert models.BlobModel.metadata is Base.metadata

    schema = f"oce_model_test_{uuid.uuid4().hex}"
    engine = create_async_engine(db_url, echo=True)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            await conn.run_sync(
                lambda sync_conn: Base.metadata.create_all(
                    sync_conn.execution_options(
                        schema_translate_map={None: schema},
                    )
                )
            )
        async with engine.connect() as conn:
            tables = set(
                await conn.scalars(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = :schema"
                    ),
                    {"schema": schema},
                )
            )
        assert {"blobs", "chunks", "blob_chunks", "chains", "chain_members"} <= tables
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()
