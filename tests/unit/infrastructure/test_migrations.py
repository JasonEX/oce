"""Programmatic SQLite schema initialization for personal mode."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

import oce

_SCRIPT_LOCATION = Path(oce.__file__).resolve().parent / "alembic"


def _head_revision() -> str:
    """The migration head, read from the chain so new migrations need no test edit."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPT_LOCATION))
    return ScriptDirectory.from_config(cfg).get_current_head()


@pytest.fixture
def sqlite_url(
    tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> str:
    """A temporary SQLite file that settings and the environment point at."""
    url = f"sqlite+aiosqlite:///{(tmp_path / 'oce.db').as_posix()}"
    monkeypatch.setenv("DB_URL", url)
    from oce.shared.config import get_settings

    get_settings.cache_clear()
    yield url
    get_settings.cache_clear()


def _sync_engine(url: str):
    return create_engine(url.replace("sqlite+aiosqlite://", "sqlite://", 1))


def test_run_migrations_creates_head_schema(sqlite_url: str) -> None:
    from oce.infrastructure.persistence.migrations import run_migrations

    run_migrations()

    engine = _sync_engine(sqlite_url)
    try:
        with engine.begin() as conn:
            inspector = inspect(conn)
            tables = set(inspector.get_table_names())
            retrieval_columns = {
                column["name"] for column in inspector.get_columns("retrieval_metrics")
            }
            blob_chunk_columns = {
                column["name"] for column in inspector.get_columns("blob_chunks")
            }
            occurrence_columns = {
                column["name"] for column in inspector.get_columns("symbol_occurrences")
            }
            occurrence_unique = [
                tuple(constraint["column_names"])
                for constraint in inspector.get_unique_constraints("symbol_occurrences")
            ]
            version = conn.execute(
                text("SELECT version_num FROM oce_alembic_version")
            ).scalar()
    finally:
        engine.dispose()

    assert {
        "blobs",
        "chunks",
        "blob_chunks",
        "symbol_occurrences",
        "index_profiles",
        "chunk_lexical",
    }.issubset(tables)
    # The monitoring tables (retrieval audit included) exist too.
    assert {
        "api_call_metrics",
        "token_usage_metrics",
        "resource_samples",
        "retrieval_metrics",
    }.issubset(tables)
    assert "rerank_route" in retrieval_columns
    assert "dense_route" in retrieval_columns
    assert {
        "exact_definitions",
        "definition_sites",
        "relation_hits",
        "relation_chars",
    } <= retrieval_columns
    assert "enclosing" in occurrence_columns
    assert (
        "identifier",
        "blob_name",
        "content_hash",
        "kind",
        "enclosing",
    ) in occurrence_unique
    assert {
        "embed_ms",
        "path_ms",
        "path_lookup_ms",
        "lexical_ms",
        "expand_ms",
        "head_slots",
    }.issubset(retrieval_columns)
    assert "lane_failures" in retrieval_columns
    assert "intent_ms" not in retrieval_columns
    assert "context" in blob_chunk_columns
    assert version == _head_revision()


def test_run_migrations_is_idempotent(sqlite_url: str) -> None:
    from oce.infrastructure.persistence.migrations import run_migrations

    run_migrations()
    run_migrations()  # no "table already exists" the second time


def test_pending_replay_index_upgrade_preserves_rows_and_avoids_sort(
    sqlite_url: str,
) -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPT_LOCATION))
    previous = "d5e6f7a8b9c0"
    command.upgrade(cfg, previous)
    engine = _sync_engine(sqlite_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO blobs (blob_name, path, content_size, file_type, status) "
                    "VALUES ('blob-1', 'src/a.py', 10, 'text', 'pending')"
                )
            )
        command.upgrade(cfg, "head")
        with engine.begin() as connection:
            indexes = {
                index["name"]: index["column_names"]
                for index in inspect(connection).get_indexes("blobs")
            }
            plan = " ".join(
                row[3]
                for row in connection.execute(
                    text(
                        "EXPLAIN QUERY PLAN SELECT blob_name FROM blobs "
                        "WHERE status = 'pending' AND blob_name > 'blob-0' "
                        "ORDER BY blob_name LIMIT 100"
                    )
                )
            )
            assert indexes["ix_blobs_status_name"] == ["status", "blob_name"]
            assert "ix_blobs_status" not in indexes
            assert "ix_blobs_status_name" in plan
            assert "TEMP B-TREE" not in plan
            assert connection.execute(text("SELECT blob_name FROM blobs")).scalar() == (
                "blob-1"
            )
        command.downgrade(cfg, previous)
        with engine.begin() as connection:
            indexes = {
                index["name"] for index in inspect(connection).get_indexes("blobs")
            }
            assert "ix_blobs_status" in indexes
            assert "ix_blobs_status_name" not in indexes
            assert connection.execute(text("SELECT blob_name FROM blobs")).scalar() == (
                "blob-1"
            )
    finally:
        engine.dispose()


def test_migration_chain_round_trips_head_base_head(sqlite_url: str) -> None:
    """A release rollback must leave the migration chain upgradeable again."""
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPT_LOCATION))

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")

    engine = _sync_engine(sqlite_url)
    try:
        with engine.begin() as connection:
            tables_after_rollback = set(inspect(connection).get_table_names())
    finally:
        engine.dispose()
    assert tables_after_rollback <= {"oce_alembic_version"}

    command.upgrade(cfg, "head")
    engine = _sync_engine(sqlite_url)
    try:
        with engine.begin() as connection:
            version = connection.execute(
                text("SELECT version_num FROM oce_alembic_version")
            ).scalar()
    finally:
        engine.dispose()
    assert version == _head_revision()


def test_symbol_occurrences_insert_auto_id_on_sqlite(sqlite_url: str) -> None:
    """Regression: ids must be INTEGER autoincrement and created_at must use func.now()."""
    from oce.infrastructure.persistence.migrations import run_migrations

    run_migrations()

    engine = _sync_engine(sqlite_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO symbol_occurrences "
                    "(identifier, blob_name, content_hash, kind, start_line, end_line) "
                    "VALUES ('parse_document', 'blob-1', 'hash-1', 'definition', 1, 5)"
                )
            )
            row = conn.execute(text("SELECT id FROM symbol_occurrences")).fetchone()
    finally:
        engine.dispose()

    assert row is not None
    assert row.id == 1
