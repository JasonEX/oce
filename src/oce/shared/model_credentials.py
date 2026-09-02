"""Shared data contract and persistence port for model credential administration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class CredentialRecord:
    """Credential view that exposes only the API key's final four characters.

    Provider-specific fields are ``None`` when they do not apply to the credential kind.
    """

    id: int
    kind: str
    provider: str | None
    name: str
    status: str
    priority: int
    endpoint: str | None
    model: str | None
    timeout_seconds: int
    note: str | None
    dimensions: int | None
    max_batch_size: int | None
    max_batch_chars: int | None
    max_input_chars: int | None
    input_overlap_chars: int | None
    top_n: int | None
    min_score: float | None
    tpm_limit: int | None
    api_key_last4: str
    created_at: datetime | None
    updated_at: datetime | None


@dataclass(frozen=True, kw_only=True)
class CredentialPatch:
    """Field overrides in which ``None`` keeps the existing value.

    Used both for partial updates and for cloning: when duplicating, ``None``
    inherits the source row, including reuse of the source API key.
    ``CredentialCreate`` shares this field set so the two cannot drift apart.
    """

    kind: str | None = None
    name: str | None = None
    api_key: str | None = None
    provider: str | None = None
    status: str | None = None
    priority: int | None = None
    endpoint: str | None = None
    model: str | None = None
    timeout_seconds: int | None = None
    note: str | None = None
    dimensions: int | None = None
    max_batch_size: int | None = None
    max_batch_chars: int | None = None
    max_input_chars: int | None = None
    input_overlap_chars: int | None = None
    top_n: int | None = None
    min_score: float | None = None
    tpm_limit: int | None = None


@dataclass(frozen=True, kw_only=True)
class CredentialCreate(CredentialPatch):
    """A complete new credential: the identifying fields are required here."""

    kind: str
    name: str
    api_key: str
    status: str = "active"
    priority: int = 100
    timeout_seconds: int = 30


class CredentialAdminStore(Protocol):
    async def list(self) -> list[CredentialRecord]: ...
    async def create(self, data: CredentialCreate) -> CredentialRecord: ...
    async def update(
        self, credential_id: int, changes: CredentialPatch
    ) -> CredentialRecord | None: ...
    async def delete(self, credential_id: int) -> bool: ...
    async def duplicate(
        self, credential_id: int, changes: CredentialPatch
    ) -> CredentialRecord | None: ...
