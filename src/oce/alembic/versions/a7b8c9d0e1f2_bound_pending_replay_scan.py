"""Support bounded pending replay with a status/name keyset index.

Revision ID: a7b8c9d0e1f2
Revises: d5e6f7a8b9c0
Create Date: 2026-09-30 12:00:00.000000

The composite index also serves status-only lookups. This is an access-path
change; stored chunks, vectors, and index-profile fingerprints are unchanged.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "a7b8c9d0e1f2"
down_revision: str | None = "d5e6f7a8b9c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_blobs_status_name", "blobs", ["status", "blob_name"])
    op.drop_index("ix_blobs_status", table_name="blobs")


def downgrade() -> None:
    op.create_index("ix_blobs_status", "blobs", ["status"])
    op.drop_index("ix_blobs_status_name", table_name="blobs")
