"""record whether vector recall ran or the SQL lanes answered first

Revision ID: c4d5e6f7a8b9
Revises: f6a7b8c9d0e1
Create Date: 2026-09-05 09:00:00.000000

``dense_route`` mirrors ``rerank_route``: ``dense`` when the embedding round
trip was awaited, ``skip:<evidence>`` when a deterministic SQL lane had already
answered a symbol/path/reference request. Joined with the stage columns it
shows how much latency the skip saved per intent.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4d5e6f7a8b9"
down_revision: str | None = "f6a7b8c9d0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("retrieval_metrics") as batch:
        batch.add_column(sa.Column("dense_route", sa.String(48), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("retrieval_metrics") as batch:
        batch.drop_column("dense_route")
