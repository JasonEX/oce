"""record which rerankers a retrieval applied

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-09-02 14:00:00.000000

rerank_ms 无法区分「按策略跳过」和「reranker 未启用」，评测 adaptive 路由需要
逐次检索的路由标签（dedicated / dedicated+llm / skip:<reason>）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("retrieval_metrics") as batch:
        batch.add_column(sa.Column("rerank_route", sa.String(length=48), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("retrieval_metrics") as batch:
        batch.drop_column("rerank_route")
