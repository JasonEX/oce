"""record the lanes a retrieval skipped after a failure

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-09-13 09:00:00.000000

A recall or relation lane that raises is logged and skipped so the request
still answers; the response carries no trace of it. The column names those
lanes with their exception type so offline calibration can tell a genuine
ranking change from an index that answered from fewer lanes.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5e6f7a8b9c0"
down_revision: str | None = "c4d5e6f7a8b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("retrieval_metrics") as batch:
        batch.add_column(sa.Column("lane_failures", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("retrieval_metrics") as batch:
        batch.drop_column("lane_failures")
