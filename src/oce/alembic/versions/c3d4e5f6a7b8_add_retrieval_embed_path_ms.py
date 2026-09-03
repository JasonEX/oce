"""record query embedding and path index latency per retrieval

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-09-02 16:00:00.000000

dense_ms 曾把 query embedding 与向量召回合在一起；path、path lookup、
lexical 和 expand 阶段虽有计时却没有列可落，路由效用无法从服务端审计解释。
head_slots 记录确定性结构证据实际占用的头部槽位。intent_ms 属于已移除的
LLM 意图分类阶段，一并清掉。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a7b8"
down_revision: str | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("retrieval_metrics") as batch:
        batch.add_column(sa.Column("embed_ms", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("path_ms", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("path_lookup_ms", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("lexical_ms", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("expand_ms", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column("head_slots", sa.Integer(), server_default="0", nullable=False)
        )
        batch.drop_column("intent_ms")


def downgrade() -> None:
    with op.batch_alter_table("retrieval_metrics") as batch:
        batch.add_column(sa.Column("intent_ms", sa.Integer(), nullable=True))
        batch.drop_column("head_slots")
        batch.drop_column("expand_ms")
        batch.drop_column("lexical_ms")
        batch.drop_column("path_lookup_ms")
        batch.drop_column("path_ms")
        batch.drop_column("embed_ms")
