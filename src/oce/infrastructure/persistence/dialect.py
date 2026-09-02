"""Dialect-specific SQL constructs shared by the repositories."""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession


def upsert_insert(session: AsyncSession):
    """Return the ``INSERT ... ON CONFLICT`` constructor for the session's dialect."""
    return sqlite_insert if session.get_bind().dialect.name == "sqlite" else pg_insert
