"""The Blob aggregate: one uploaded file.

A blob carries the file's metadata (path, status, last_seen), its chunk
references and the pending -> ready/error state machine. ``blob_name`` must
be a SHA256; an empty or binary file may have no chunks and still be ready.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING

from oce.shared.hashes import is_sha256_hex

if TYPE_CHECKING:
    from oce.domain.chunk import ChunkRef


class BlobStatus(str, Enum):
    PENDING = "pending"  # waiting to be embedded
    READY = "ready"  # retrievable
    ERROR = "error"  # embedding failed


@dataclass
class Blob:
    blob_name: str  # primary key: SHA256(path + content)
    path: str  # repository-relative path
    status: BlobStatus = BlobStatus.PENDING
    chunks: list[ChunkRef] = field(default_factory=list)
    content_size: int = 0
    language: str | None = None
    file_type: str = "text"
    retry_count: int = 0
    last_seen: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    error_message: str | None = None

    def __post_init__(self) -> None:
        if not is_sha256_hex(self.blob_name):
            raise ValueError(f"Invalid blob_name (not SHA256): {self.blob_name}")

    def mark_ready(self) -> None:
        """Mark the blob retrievable; an empty text file completes indexing too."""
        self.status = BlobStatus.READY
        self.error_message = None

    def mark_error(self, error_message: str) -> None:
        self.status = BlobStatus.ERROR
        self.error_message = error_message

    def increment_retry(self, max_retries: int = 3) -> bool:
        """Count one retry; past the limit the blob is marked failed. Returns whether it was."""
        self.retry_count += 1
        if self.retry_count >= max_retries:
            self.mark_error(f"Failed after {self.retry_count} retries")
            return True
        return False

    def touch(self) -> None:
        self.last_seen = datetime.now(timezone.utc)

    def is_ready(self) -> bool:
        return self.status == BlobStatus.READY

    def __repr__(self) -> str:
        return (
            f"Blob(name={self.blob_name[:8]}..., path={self.path}, "
            f"status={self.status.value}, chunks={len(self.chunks)})"
        )
