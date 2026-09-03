"""add chunk context and lexical index

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-09-02 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from oce.infrastructure.persistence.lexical_index import (
    create_lexical_table,
    drop_lexical_table,
)


# revision identifiers, used by Alembic.
revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # blob_chunks.context: the enclosing scope chain of one chunk occurrence.
    op.add_column("blob_chunks", sa.Column("context", sa.Text(), nullable=True))
    # chunk_lexical: FTS5 virtual table on SQLite, tsvector + GIN on PostgreSQL.
    # The DDL lives with the store so tests build the same table.
    create_lexical_table(op.get_bind())


def downgrade() -> None:
    drop_lexical_table(op.get_bind())
    with op.batch_alter_table("blob_chunks") as batch_op:
        batch_op.drop_column("context")
