"""relax model_credentials unique key to (kind, model, api_key_hash)

Revision ID: e2f4a6c8d0b1
Revises: b7c9e1f2a3d4
Create Date: 2026-09-01 09:45:00.000000

One key serving several kinds and models is the norm: the unique constraint
widens from (kind, api_key_hash) to (kind, model, api_key_hash), so the same
key may be used across kinds and with different models under one kind, and
only an exact duplicate is rejected. The new constraint is a superset of the
old one, so existing rows cannot conflict. SQLite cannot alter a named
constraint and rebuilds the table in batch mode; PostgreSQL drops and adds.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'e2f4a6c8d0b1'
down_revision: Union[str, None] = 'b7c9e1f2a3d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_OLD_NAME = "uq_model_credentials_kind_key"
_NEW_NAME = "uq_model_credentials_kind_model_key"
_OLD_COLS = ["kind", "api_key_hash"]
_NEW_COLS = ["kind", "model", "api_key_hash"]


def _swap_unique(drop_name: str, create_name: str, create_cols: list[str]) -> None:
    """Replace the unique constraint ``drop_name`` with ``create_name`` over ``create_cols``."""
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("model_credentials") as batch_op:
            batch_op.drop_constraint(drop_name, type_="unique")
            batch_op.create_unique_constraint(create_name, create_cols)
    else:
        op.drop_constraint(drop_name, "model_credentials", type_="unique")
        op.create_unique_constraint(create_name, "model_credentials", create_cols)


def upgrade() -> None:
    _swap_unique(_OLD_NAME, _NEW_NAME, _NEW_COLS)


def downgrade() -> None:
    _swap_unique(_NEW_NAME, _OLD_NAME, _OLD_COLS)
