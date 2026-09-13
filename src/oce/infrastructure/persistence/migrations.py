"""Programmatic Alembic entry point used by ``oce serve`` in personal mode.

Service mode still runs ``uv run alembic upgrade head`` itself. No database
compatibility promise has been published yet, so only an empty database or
one with an Alembic version table is supported; an unknown schema is never
guessed at or stamped.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

# The migration scripts ship inside the wheel (src/oce/alembic) and are
# located by package path, so a `uv tool install` environment migrates
# without a repository checkout.
_SCRIPT_DIR = Path(__file__).resolve().parents[2] / "alembic"


def run_migrations() -> None:
    """Upgrade the database at ``DB_URL`` to the migration head."""
    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPT_DIR))

    command.upgrade(cfg, "head")
