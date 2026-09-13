"""In-memory doubles shared by the application-layer tests.

Each implements only the protocol methods the code under test calls.
"""

from __future__ import annotations

import hashlib
import uuid

from oce.domain.blob.blob import Blob, BlobStatus
from oce.domain.chain.chain import Chain
from oce.domain.chunk import LocatedChunk
from tests.fakes.indexing import FakeLexicalProjection, FakeSymbolProjection


def blob_name(path: str, content: str) -> str:
    """The production content address: SHA256(path + content)."""
    return hashlib.sha256(f"{path}{content}".encode()).hexdigest()


class FakeBlobRepo:
    """In-memory BlobRepository."""

    def __init__(self) -> None:
        self.blobs: dict[str, Blob] = {}
        self.staging: dict[str, str] = {}  # blob_name -> content

    async def save(self, blob: Blob) -> None:
        self.blobs[blob.blob_name] = blob

    async def get(self, blob_name: str) -> Blob | None:
        return self.blobs.get(blob_name)

    async def get_many(self, blob_names) -> dict[str, Blob]:
        return {n: self.blobs[n] for n in blob_names if n in self.blobs}

    async def exists_many(self, blob_names) -> dict[str, bool]:
        return {n: n in self.blobs for n in blob_names}

    async def find_pending(self, blob_names=None) -> list[Blob]:
        names = set(blob_names) if blob_names is not None else None
        return [
            b
            for b in self.blobs.values()
            if b.status == BlobStatus.PENDING
            and (names is None or b.blob_name in names)
        ]

    async def list_ready_names(self, limit: int) -> list[str]:
        return sorted(
            name for name, blob in self.blobs.items() if blob.status == BlobStatus.READY
        )[: max(0, limit)]

    async def delete(self, blob_name: str) -> None:
        self.blobs.pop(blob_name, None)

    async def delete_many(self, blob_names) -> None:
        for name in blob_names:
            self.blobs.pop(name, None)

    async def save_staging(self, blob_name: str, content: str) -> None:
        """Store the staged text."""
        self.staging[blob_name] = content

    async def get_staging(self, blob_name: str) -> str | None:
        """Read the staged text."""
        return self.staging.get(blob_name)

    async def delete_staging(self, blob_name: str) -> None:
        """Drop the staged text."""
        self.staging.pop(blob_name, None)


class FakeChainRepo:
    """In-memory ChainRepository."""

    def __init__(self) -> None:
        self.chains: dict[str, Chain] = {}

    async def create(self, members) -> Chain:
        chain = Chain(chain_id=uuid.uuid4().hex, version=1, members=set(members))
        self.chains[chain.chain_id] = chain
        return chain

    async def get(self, chain_id: str) -> Chain | None:
        return self.chains.get(chain_id)

    async def exists(self, chain_id: str, version: int | None = None) -> bool:
        chain = self.chains.get(chain_id)
        return chain is not None and (version is None or chain.version == version)

    async def get_members(self, chain_id: str) -> set[str]:
        chain = self.chains.get(chain_id)
        return set(chain.members) if chain else set()

    async def apply_checkpoint(
        self, chain_id, expected_version, added, deleted
    ) -> int | None:
        chain = self.chains.get(chain_id)
        if chain is None or chain.version != expected_version:
            return None
        chain.members = (chain.members | set(added)) - set(deleted)
        chain.version += 1
        return chain.version

    async def touch_members(self, chain_id: str) -> None:
        pass


class FakeChunkRepo:
    """In-memory ChunkRepository."""

    def __init__(self, blob_repo: FakeBlobRepo) -> None:
        self.blob_repo = blob_repo
        self.chunks: dict[str, object] = {}
        self.pending: list[object] = []

    async def save_many(self, chunks) -> None:
        for c in chunks:
            self.chunks[c.content_hash] = c
            # A newly saved chunk is pending until marked embedded.
            if c not in self.pending:
                self.pending.append(c)

    async def find_pending_for_blobs(self, blob_names, limit=None) -> list[object]:
        pending_hashes = {chunk.content_hash for chunk in self.pending}
        result = []
        for blob_name in blob_names:
            blob = self.blob_repo.blobs.get(blob_name)
            if blob is None or blob.status != BlobStatus.PENDING:
                continue
            for ref in blob.chunks:
                chunk = self.chunks.get(ref.content_hash)
                if chunk is None or ref.content_hash not in pending_hashes:
                    continue
                result.append(
                    LocatedChunk(
                        blob_name=blob_name,
                        content_hash=ref.content_hash,
                        path=blob.path,
                        content=chunk.content,
                        start_line=ref.start_line,
                        end_line=ref.end_line,
                    )
                )
        return result if limit is None else result[:limit]

    async def mark_embedded(self, content_hashes: list[str]) -> None:
        """Drop the chunks from the pending list."""
        hashes = set(content_hashes)
        self.pending = [c for c in self.pending if c.content_hash not in hashes]


class FakeUnitOfWork:
    def __init__(self) -> None:
        self.blobs = FakeBlobRepo()
        self.chunks = FakeChunkRepo(self.blobs)
        self.chains = FakeChainRepo()
        self.symbols = FakeSymbolProjection()
        self.lexical = FakeLexicalProjection()
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None


class FakeUnitOfWorkFactory:
    def __init__(self, uow: FakeUnitOfWork | None = None) -> None:
        self.uow = uow or FakeUnitOfWork()

    def __call__(self) -> FakeUnitOfWork:
        return self.uow
