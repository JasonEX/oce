"""Remove metadata left behind by SQLite connections without foreign keys.

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-10-01 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b8c9d0e1f2a3"
down_revision: str | None = "a7b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "DELETE FROM blob_staging WHERE NOT EXISTS "
        "(SELECT 1 FROM blobs WHERE blobs.blob_name = blob_staging.blob_name)"
    )
    for table in ("symbol_occurrences", "blob_chunks"):
        op.execute(
            f"DELETE FROM {table} WHERE NOT EXISTS "
            f"(SELECT 1 FROM blobs WHERE blobs.blob_name = {table}.blob_name) "
            "OR NOT EXISTS "
            f"(SELECT 1 FROM chunks WHERE chunks.content_hash = {table}.content_hash)"
        )
    op.execute(
        "DELETE FROM chunk_lexical WHERE NOT EXISTS "
        "(SELECT 1 FROM chunks WHERE chunks.content_hash = chunk_lexical.content_hash)"
    )


def downgrade() -> None:
    # Deleted orphaned data had no surviving source of truth to restore.
    pass
