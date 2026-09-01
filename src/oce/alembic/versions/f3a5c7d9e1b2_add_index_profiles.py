"""add persisted index lifecycle profile

Revision ID: f3a5c7d9e1b2
Revises: e2f4a6c8d0b1
Create Date: 2026-09-01 13:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "f3a5c7d9e1b2"
down_revision: Union[str, None] = "e2f4a6c8d0b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "index_profiles",
        sa.Column("profile_key", sa.String(length=16), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("profile_json", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("profile_key"),
    )


def downgrade() -> None:
    op.drop_table("index_profiles")
