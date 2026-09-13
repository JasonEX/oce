"""redesign credentials into multi-kind model_credentials

Revision ID: b7c9e1f2a3d4
Revises: a2d4f6b8c1e3
Create Date: 2026-08-29 11:00:00.000000

Replace the embed/rerank-only embedding_credentials (+embedding_providers)
with the multi-kind model_credentials table: one row per (kind, account)
channel, kind in embed/rerank/llm_rerank/query_rewrite/intent. No data is
migrated: the old tables are dropped and the new one created.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7c9e1f2a3d4'
down_revision: Union[str, None] = 'a2d4f6b8c1e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Two prior states exist: the released chain (embedding_credentials plus
    # embedding_providers) and development databases that applied an
    # unreleased flatten (embedding_credentials alone). Credentials carry no
    # data worth keeping, so both are dropped IF EXISTS, child before parent.
    op.execute("DROP TABLE IF EXISTS embedding_credentials")
    op.execute("DROP TABLE IF EXISTS embedding_providers")

    op.create_table(
        'model_credentials',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('provider', sa.String(length=64), nullable=True),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('api_key', sa.String(length=512), nullable=False),
        sa.Column('api_key_hash', sa.String(length=64), nullable=False),
        sa.Column('endpoint', sa.String(length=512), nullable=True),
        sa.Column('model', sa.String(length=128), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('priority', sa.Integer(), nullable=False),
        sa.Column('timeout_seconds', sa.Integer(), nullable=False),
        sa.Column('rate_limit', sa.Integer(), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('dimensions', sa.Integer(), nullable=True),
        sa.Column('max_batch_size', sa.Integer(), nullable=True),
        sa.Column('max_batch_chars', sa.Integer(), nullable=True),
        sa.Column('max_input_chars', sa.Integer(), nullable=True),
        sa.Column('input_overlap_chars', sa.Integer(), nullable=True),
        sa.Column('top_n', sa.Integer(), nullable=True),
        sa.Column('min_score', sa.Float(), nullable=True),
        sa.Column('tpm_limit', sa.Integer(), nullable=True),
        sa.Column('max_candidates', sa.Integer(), nullable=True),
        sa.Column('output_top_k', sa.Integer(), nullable=True),
        sa.Column('snippet_chars', sa.Integer(), nullable=True),
        sa.Column('num_rewrites', sa.Integer(), nullable=True),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('kind', 'api_key_hash', name='uq_model_credentials_kind_key'),
    )
    op.create_index(
        'idx_model_credentials_kind_status_priority',
        'model_credentials',
        ['kind', 'status', 'priority'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        'idx_model_credentials_kind_status_priority',
        table_name='model_credentials',
    )
    op.drop_table('model_credentials')

    op.create_table(
        'embedding_providers',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('code', sa.String(length=64), nullable=False),
        sa.Column('display_name', sa.String(length=128), nullable=False),
        sa.Column('embed_endpoint', sa.String(length=512), nullable=True),
        sa.Column('embed_model', sa.String(length=128), nullable=True),
        sa.Column('rerank_endpoint', sa.String(length=512), nullable=True),
        sa.Column('rerank_model', sa.String(length=128), nullable=True),
        sa.Column('dimensions', sa.Integer(), nullable=False),
        sa.Column('max_batch_size', sa.Integer(), nullable=False),
        sa.Column('max_batch_chars', sa.Integer(), nullable=False),
        sa.Column('max_input_chars', sa.Integer(), nullable=False),
        sa.Column('input_overlap_chars', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code'),
    )
    op.create_table(
        'embedding_credentials',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('provider_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('api_key', sa.String(length=512), nullable=False),
        sa.Column('api_key_hash', sa.String(length=64), nullable=False),
        sa.Column('priority', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('max_batch_size', sa.Integer(), nullable=True),
        sa.Column('max_batch_chars', sa.Integer(), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('rate_limit', sa.Integer(), nullable=True),
        sa.Column('timeout_seconds', sa.Integer(), nullable=False),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['provider_id'], ['embedding_providers.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('api_key_hash'),
    )
    op.create_index('idx_embedding_credentials_priority', 'embedding_credentials', ['priority'], unique=False)
    op.create_index('idx_embedding_credentials_status', 'embedding_credentials', ['status'], unique=False)
    op.create_index('idx_embedding_credentials_provider_id', 'embedding_credentials', ['provider_id'], unique=False)
