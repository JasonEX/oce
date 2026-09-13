"""Apply one ``SearchScope`` to SQL statements the same way in every store.

A checkpoint scope is applied as a relation (an ``IN`` subquery over ``chain_members``)
so large workspaces never expand into one ``IN (...)`` clause. Added-only
scopes, unusually large request deltas, and a checkpoint that moved between
scope resolution and the query fall back to bounded ``IN`` batches over the
materialized member list.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select
from sqlalchemy.sql.elements import ColumnElement

from oce.domain.services.search import SearchScope
from oce.infrastructure.persistence.models import ChainMemberModel, ChainModel

SCOPE_BATCH_SIZE = 500
RELATIONAL_DELTA_LIMIT = 500

StatementBuilder = Callable[[ColumnElement[bool]], Select[Any]]


def relational_predicate(
    scope: SearchScope, blob_column: Any
) -> ColumnElement[bool] | None:
    """Membership predicate over the checkpoint chain, or ``None`` when the scope
    has no chain or its request delta is too large to express inline."""
    delta_size = len(scope.added_blob_names) + len(scope.deleted_blob_names)
    if (
        scope.chain_id is None
        or scope.chain_version is None
        or delta_size > RELATIONAL_DELTA_LIMIT
    ):
        return None
    membership: ColumnElement[bool] = blob_column.in_(
        select(ChainMemberModel.blob_name)
        .select_from(ChainMemberModel)
        .join(ChainModel, ChainModel.chain_id == ChainMemberModel.chain_id)
        .where(
            ChainMemberModel.chain_id == scope.chain_id,
            ChainModel.version == scope.chain_version,
        )
    )
    if scope.added_blob_names:
        membership = or_(membership, blob_column.in_(scope.added_blob_names))
    if scope.deleted_blob_names:
        membership = and_(membership, blob_column.not_in(scope.deleted_blob_names))
    return membership


async def run_scoped(
    session: AsyncSession,
    scope: SearchScope,
    blob_column: Any,
    build: StatementBuilder,
) -> list[Row[Any]]:
    """Execute ``build(predicate)`` under the scope; rows from batches are concatenated."""
    predicate = relational_predicate(scope, blob_column)
    if predicate is not None:
        relational_rows = list((await session.execute(build(predicate))).all())
        current_version = await session.scalar(
            select(ChainModel.version).where(ChainModel.chain_id == scope.chain_id)
        )
        if current_version == scope.chain_version:
            return relational_rows

    rows: list[Row[Any]] = []
    names: Sequence[str] = sorted(scope.blob_names)
    for offset in range(0, len(names), SCOPE_BATCH_SIZE):
        batch = names[offset : offset + SCOPE_BATCH_SIZE]
        rows.extend((await session.execute(build(blob_column.in_(batch)))).all())
    return rows
