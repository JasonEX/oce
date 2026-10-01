"""Repository protocols, implemented by infrastructure/persistence."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from oce.domain.blob.blob import Blob
    from oce.domain.chain.chain import Chain
    from oce.domain.chunk import Chunk, LocatedChunk
    from oce.domain.services.search import SearchScope


class BlobRepository(Protocol):
    async def get(self, blob_name: str) -> Blob | None: ...

    async def get_many(self, blob_names: Sequence[str]) -> dict[str, Blob]: ...

    async def exists_many(self, blob_names: Sequence[str]) -> dict[str, bool]: ...

    async def ready_names(self, scope: SearchScope) -> set[str]:
        """Ready identities from a working set, without loading chunk references."""
        ...

    async def save(self, blob: Blob) -> None: ...

    async def save_many(self, blobs: Sequence[Blob]) -> None: ...

    async def touch(self, blob_name: str) -> None:
        """Refresh activity without changing indexing state or chunk references."""
        ...

    async def delete(self, blob_name: str) -> None: ...

    async def delete_many(self, blob_names: Sequence[str]) -> None: ...

    async def mark_deleting(
        self, blob_names: Sequence[str], *, ttl_days: int | None = None
    ) -> list[str]:
        """Retain unreferenced identities until every vector store deletes them."""
        ...

    async def delete_deleting(self, blob_names: Sequence[str]) -> None:
        """Remove only identities still owned by the deletion lifecycle."""
        ...

    async def find_pending(
        self, blob_names: Sequence[str] | None = None
    ) -> list[Blob]: ...

    async def list_pending_names(
        self, *, limit: int | None = None, after: str | None = None
    ) -> list[str]:
        """Pending identities in name order, optionally one bounded keyset page."""
        ...

    async def count_pending(self) -> int:
        """Count pending tasks without loading their identities."""
        ...

    async def list_ready_names(self, limit: int) -> list[str]:
        """A bounded sample of indexed blobs for storage initialization."""
        ...

    async def find_expired(self, ttl_days: int, batch_size: int = 1000) -> list[str]:
        """Blobs past the TTL that no checkpoint chain references."""
        ...

    # blob_staging: the raw text buffer the worker chunks from
    async def get_staging(self, blob_name: str) -> str | None: ...
    async def save_staging(self, blob_name: str, content: str) -> None: ...
    async def delete_staging(self, blob_name: str) -> None: ...

    async def find_stale_with_staging(
        self,
        stale_hours: int = 24,
        limit: int = 100,
    ) -> list[str]:
        """Pending blobs with staging text that have waited too long, for requeueing."""
        ...


class ChainRepository(Protocol):
    async def get(self, chain_id: str) -> Chain | None: ...

    async def exists(self, chain_id: str, version: int | None = None) -> bool: ...

    async def create(self, members: Sequence[str]) -> Chain: ...

    async def get_members(self, chain_id: str) -> set[str]: ...

    async def apply_checkpoint(
        self,
        chain_id: str,
        expected_version: int,
        added: Sequence[str],
        deleted: Sequence[str],
    ) -> int | None: ...

    async def touch_members(self, chain_id: str) -> None: ...

    async def delete(self, chain_id: str) -> None: ...

    async def find_expired(self, ttl_days: int) -> list[str]: ...


class ChunkRepository(Protocol):
    async def save_many(self, chunks: Sequence[Chunk]) -> None: ...

    async def mark_embedded(self, content_hashes: Sequence[str]) -> None: ...

    async def find_pending_for_blobs(
        self,
        blob_names: Sequence[str],
        limit: int | None = None,
    ) -> list[LocatedChunk]: ...
