"""Resolve the credential row a runtime should use for one ``kind``."""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oce.infrastructure.persistence.models import ModelCredentialModel


async def resolve_active_credential(
    session_factory: Callable[[], AsyncSession],
    kind: str,
    *,
    require_endpoint_and_model: bool = False,
) -> ModelCredentialModel | None:
    """Return the active credential with the smallest priority, or None.

    Embedding and rerank rows are only usable with a full endpoint and model;
    chat rows may leave both empty and fall back to the LLM_* settings.
    """
    conditions = [
        ModelCredentialModel.kind == kind,
        ModelCredentialModel.status == "active",
    ]
    if require_endpoint_and_model:
        conditions.extend(
            (
                ModelCredentialModel.endpoint.is_not(None),
                ModelCredentialModel.model.is_not(None),
            )
        )
    statement = (
        select(ModelCredentialModel)
        .where(*conditions)
        .order_by(ModelCredentialModel.priority, ModelCredentialModel.id)
        .limit(1)
    )
    async with session_factory() as session:
        return (await session.execute(statement)).scalars().first()
