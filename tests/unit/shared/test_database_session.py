"""Database-engine policy shared by personal-mode persistence and metrics."""

from sqlalchemy import text

from oce.shared.config.settings import DatabaseSettings
from oce.shared.database.session import create_engine


async def test_sqlite_engine_uses_wal_and_busy_timeout(tmp_path) -> None:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'wal.db').as_posix()}"
    engine = create_engine(DatabaseSettings(url=url))
    try:
        async with engine.connect() as connection:
            journal = (await connection.execute(text("PRAGMA journal_mode"))).scalar()
            timeout = (await connection.execute(text("PRAGMA busy_timeout"))).scalar()
    finally:
        await engine.dispose()

    assert journal == "wal"
    assert timeout == 5000
