"""Dialect-specific SQL constructs shared by the repositories."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy.dialects.postgresql import Insert as PgInsert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import Insert as SqliteInsert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession


def upsert_insert(
    session: AsyncSession,
) -> Callable[[Any], SqliteInsert | PgInsert]:
    """Return the ``INSERT ... ON CONFLICT`` constructor for the session's dialect."""
    return sqlite_insert if session.get_bind().dialect.name == "sqlite" else pg_insert
