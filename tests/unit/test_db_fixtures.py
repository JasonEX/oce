"""The SQLite database fixtures work."""

import pytest
from sqlalchemy import text


@pytest.mark.asyncio
async def test_db_engine_fixture(test_engine):
    """The engine fixture."""
    assert test_engine is not None
    assert test_engine.dialect.name == "sqlite"


@pytest.mark.asyncio
async def test_db_session_fixture(test_session):
    """The session fixture."""
    assert test_session is not None

    result = await test_session.execute(text("SELECT 1"))
    assert result.scalar() == 1


@pytest.mark.asyncio
async def test_db_session_isolation(test_session):
    """Sessions are isolated per test."""
    assert test_session is not None

    result = await test_session.execute(text("SELECT 1"))
    assert result.scalar() == 1
