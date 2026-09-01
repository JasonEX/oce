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
    rate_limit: int | None
    note: str | None
    dimensions: int | None
    max_batch_size: int | None
    max_batch_chars: int | None
    max_input_chars: int | None
    input_overlap_chars: int | None
    top_n: int | None
    min_score: float | None
    tpm_limit: int | None
    max_candidates: int | None
    output_top_k: int | None
    snippet_chars: int | None
    num_rewrites: int | None
    api_key_last4: str
    last_used_at: datetime | None
    created_at: datetime | None
    updated_at: datetime | None


@dataclass(frozen=True)
class CredentialCreate:
    kind: str
    name: str
    api_key: str
    provider: str | None = None
    status: str = "active"
    priority: int = 100
    endpoint: str | None = None
    model: str | None = None
    timeout_seconds: int = 30
    rate_limit: int | None = None
    note: str | None = None
    dimensions: int | None = None
    max_batch_size: int | None = None
    max_batch_chars: int | None = None
    max_input_chars: int | None = None
    input_overlap_chars: int | None = None
    top_n: int | None = None
    min_score: float | None = None
    tpm_limit: int | None = None
    max_candidates: int | None = None
    output_top_k: int | None = None
    snippet_chars: int | None = None
    num_rewrites: int | None = None


@dataclass(frozen=True)
class CredentialUpdate:
    """Partial update in which ``None`` leaves the stored field unchanged."""

    kind: str | None = None
    name: str | None = None
    api_key: str | None = None
    provider: str | None = None
    status: str | None = None
    priority: int | None = None
    endpoint: str | None = None
    model: str | None = None
    timeout_seconds: int | None = None
    rate_limit: int | None = None
    note: str | None = None
    dimensions: int | None = None
    max_batch_size: int | None = None
    max_batch_chars: int | None = None
    max_input_chars: int | None = None
    input_overlap_chars: int | None = None
    top_n: int | None = None
    min_score: float | None = None
    tpm_limit: int | None = None
    max_candidates: int | None = None
    output_top_k: int | None = None
    snippet_chars: int | None = None
    num_rewrites: int | None = None


@dataclass(frozen=True)
class CredentialDuplicate:
    """Optional overrides applied while cloning an existing credential.

    ``None`` inherits the source value, including reuse of the source API key.
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
    rate_limit: int | None = None
    note: str | None = None
    dimensions: int | None = None
    max_batch_size: int | None = None
    max_batch_chars: int | None = None
    max_input_chars: int | None = None
    input_overlap_chars: int | None = None
    top_n: int | None = None
    min_score: float | None = None
    tpm_limit: int | None = None
    max_candidates: int | None = None
    output_top_k: int | None = None
    snippet_chars: int | None = None
    num_rewrites: int | None = None


class CredentialAdminStore(Protocol):
    async def list(self) -> list[CredentialRecord]: ...
    async def create(self, data: CredentialCreate) -> CredentialRecord: ...
    async def update(
        self, credential_id: int, changes: CredentialUpdate
    ) -> CredentialRecord | None: ...
    async def delete(self, credential_id: int) -> bool: ...
    async def duplicate(
        self, credential_id: int, changes: CredentialDuplicate
    ) -> CredentialRecord | None: ...
