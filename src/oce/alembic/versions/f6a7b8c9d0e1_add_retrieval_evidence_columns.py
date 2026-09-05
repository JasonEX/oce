"""record structural evidence and relation-section size per retrieval

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-09-04 12:30:00.000000

``rerank_route`` says which reranker ran; these columns say what the router
saw (definitions found, how many places declare the name) and how much
relation evidence was appended, so offline case results can be joined with
routing decisions when the adaptive thresholds are calibrated.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f6a7b8c9d0e1"
down_revision: str | None = "e5f6a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = ("exact_definitions", "definition_sites", "relation_hits", "relation_chars")


def upgrade() -> None:
    with op.batch_alter_table("retrieval_metrics") as batch:
        for name in _COLUMNS:
            batch.add_column(
                sa.Column(name, sa.Integer(), server_default="0", nullable=False)
            )


def downgrade() -> None:
    with op.batch_alter_table("retrieval_metrics") as batch:
        for name in reversed(_COLUMNS):
            batch.drop_column(name)
