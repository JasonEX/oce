"""record the enclosing definition of every symbol occurrence

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-09-04 12:00:00.000000

A call row alone says "this chunk invokes X"; with the enclosing definition it
becomes the edge "caller -> X" that inbound relation lookups need. The column
joins the unique key so two functions in one chunk calling the same symbol keep
both edges. Existing rows get the empty string, which the extraction version
bump (``SYMBOL_EXTRACTION_VERSION`` 5) invalidates anyway.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5f6a7b8c9d0"
down_revision: str | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("symbol_occurrences") as batch:
        batch.add_column(
            sa.Column(
                "enclosing", sa.String(length=256), server_default="", nullable=False
            )
        )
        batch.drop_constraint("uq_symbol_occurrences_key", type_="unique")
        batch.create_unique_constraint(
            "uq_symbol_occurrences_key",
            ["identifier", "blob_name", "content_hash", "kind", "enclosing"],
        )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM symbol_occurrences"))
    with op.batch_alter_table("symbol_occurrences") as batch:
        batch.drop_constraint("uq_symbol_occurrences_key", type_="unique")
        batch.create_unique_constraint(
            "uq_symbol_occurrences_key",
            ["identifier", "blob_name", "content_hash", "kind"],
        )
        batch.drop_column("enclosing")
