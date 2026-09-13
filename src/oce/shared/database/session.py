"""SQLAlchemy engine, session factory and declarative base."""

from sqlalchemy import event
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import ConnectionPoolEntry

from oce.shared.config import get_settings
from oce.shared.config.settings import DatabaseSettings


def _configure_sqlite(
    dbapi_connection: DBAPIConnection, _record: ConnectionPoolEntry
) -> None:
    """Personal mode serves uploads, retrieval, and metrics from one file.

    The default rollback journal lets a long upload transaction lock every
    other writer out: the metrics sink logged "database is locked" on each
    flush during a batch upload. WAL keeps readers and one writer concurrent,
    and the busy timeout lets a second writer wait instead of failing.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA journal_mode=WAL")
    finally:
        cursor.close()


def create_engine(settings: DatabaseSettings) -> AsyncEngine:
    if settings.is_sqlite:
        async_engine = create_async_engine(
            settings.url,
            connect_args={"check_same_thread": False},
            echo=settings.echo,
        )
        event.listen(async_engine.sync_engine, "connect", _configure_sqlite)
        return async_engine
    return create_async_engine(
        settings.url,
        echo=settings.echo,
        pool_size=settings.pool_size,
        max_overflow=settings.max_overflow,
        pool_pre_ping=True,
        pool_timeout=5,
        pool_recycle=1800,
    )


engine = create_engine(get_settings().database)
async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    """Declarative base shared by every metadata and monitoring table."""
