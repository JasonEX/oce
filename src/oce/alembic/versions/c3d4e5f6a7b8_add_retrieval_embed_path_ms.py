"""record query embedding and path index latency per retrieval

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-09-02 16:00:00.000000

dense_ms 曾把 query embedding 与向量召回合在一起，path 阶段虽有计时却没有列可落，
路径索引与远端 embedding 的延迟在审计里不可见。intent_ms 属于已移除的 LLM 意图
分类阶段，ORM 模型里早已没有这一列，一并清掉。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("retrieval_metrics") as batch:
        batch.add_column(sa.Column("embed_ms", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("path_ms", sa.Integer(), nullable=True))
        batch.drop_column("intent_ms")


def downgrade() -> None:
    with op.batch_alter_table("retrieval_metrics") as batch:
        batch.add_column(sa.Column("intent_ms", sa.Integer(), nullable=True))
        batch.drop_column("path_ms")
        batch.drop_column("embed_ms")
