"""drop model_credentials columns that no runtime ever read

Revision ID: a1b2c3d4e5f6
Revises: f3a5c7d9e1b2
Create Date: 2026-09-02 12:00:00.000000

rate_limit and last_used_at were never written or read; max_candidates,
output_top_k, snippet_chars and num_rewrites were only reachable through the
admin API while the runtime always used the LLM_* settings, so keeping them
showed the admin panel a configuration that had no effect. SQLite rebuilds
the table in batch mode.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "f3a5c7d9e1b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DROPPED = (
    ("rate_limit", sa.Integer()),
    ("max_candidates", sa.Integer()),
    ("output_top_k", sa.Integer()),
    ("snippet_chars", sa.Integer()),
    ("num_rewrites", sa.Integer()),
    ("last_used_at", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    with op.batch_alter_table("model_credentials") as batch:
        for name, _type in _DROPPED:
            batch.drop_column(name)


def downgrade() -> None:
    with op.batch_alter_table("model_credentials") as batch:
        for name, column_type in _DROPPED:
            batch.add_column(sa.Column(name, column_type, nullable=True))
