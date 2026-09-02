"""drop model_credentials columns that no runtime ever read

Revision ID: a1b2c3d4e5f6
Revises: f3a5c7d9e1b2
Create Date: 2026-09-02 12:00:00.000000

rate_limit / last_used_at 从未被写入或读取；max_candidates / output_top_k /
snippet_chars / num_rewrites 只能通过 admin API 存取，运行时始终使用 LLM_*
环境变量，保留它们会让 admin 面板呈现一个并不生效的配置。SQLite 走 batch 重建表。
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
