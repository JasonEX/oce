"""record query embedding and path index latency per retrieval

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-09-02 16:00:00.000000

dense_ms used to include the query embedding round trip; the path, path
lookup, lexical and expand stages were timed but had no column, so routing
decisions could not be explained from the server audit. head_slots records
the head slots deterministic evidence actually took. intent_ms belonged to
the removed LLM intent stage and goes with it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a7b8"
down_revision: str | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("retrieval_metrics") as batch:
        batch.add_column(sa.Column("embed_ms", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("path_ms", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("path_lookup_ms", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("lexical_ms", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("expand_ms", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column("head_slots", sa.Integer(), server_default="0", nullable=False)
        )
        batch.drop_column("intent_ms")


def downgrade() -> None:
    with op.batch_alter_table("retrieval_metrics") as batch:
        batch.add_column(sa.Column("intent_ms", sa.Integer(), nullable=True))
        batch.drop_column("head_slots")
        batch.drop_column("expand_ms")
        batch.drop_column("lexical_ms")
        batch.drop_column("path_lookup_ms")
        batch.drop_column("path_ms")
        batch.drop_column("embed_ms")
