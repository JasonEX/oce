"""IndexingPipeline: the write path.

``ingest`` stores blob metadata and the staged text. Indexing a pending blob
then runs in three phases with the remote embedding call outside any
metadata transaction:

    prepare        chunk, project symbols and terms         (transaction 1)
    write_vectors  embed, write the vectors and path index  (no transaction)
    complete       mark the chunks embedded, the blob ready (transaction 2)
    fail           mark the blob's error instead            (transaction 2)

Holding the metadata transaction across the embedding round trip kept
SQLite's single write lock for seconds, so a concurrent upload, a retrieval
that indexes its added files, or the monitoring flush failed with "database
is locked". ``complete`` and ``fail`` reload the blobs that are still pending,
so a blob another request finished or garbage collection removed in between
is neither resurrected nor marked twice. ``embed_pending`` runs the phases in
one unit of work for callers that do not commit in between.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from loguru import logger

from oce.domain.blob.blob import Blob, BlobStatus
from oce.domain.chunk import Chunker, LocatedChunk
from oce.domain.chunk.lang import detect_language
from oce.domain.repositories import BlobRepository, ChunkRepository
from oce.domain.services.embedder import Embedder
from oce.domain.services.lexical import LexicalProjection
from oce.domain.services.path_document_builder import (
    build_path_document,
    is_indexable_path,
)
from oce.domain.services.path_search import PathSearchStore
from oce.domain.services.search import VectorIndex, VectorRecord
from oce.domain.services.source_filter import is_binary_source, is_ignored_source_path
from oce.domain.services.symbols import SymbolProjection


@dataclass(frozen=True)
class PendingBatch:
    """Pending blobs that ``prepare`` chunked, and the chunks still without a vector."""

    blobs: tuple[Blob, ...]
    chunks: tuple[LocatedChunk, ...]

    @property
    def blob_names(self) -> tuple[str, ...]:
        return tuple(blob.blob_name for blob in self.blobs)


class IndexingPipeline:
    def __init__(
        self,
        *,
        chunker: Chunker,
        embedder: Embedder,
        vector_index: VectorIndex,
        blob_repo: BlobRepository,
        chunk_repo: ChunkRepository,
        symbol_projection: SymbolProjection,
        embed_batch_size: int = 256,
        path_store: PathSearchStore | None = None,
        embedding_enabled: bool = True,
        lexical_projection: LexicalProjection | None = None,
    ) -> None:
        if embed_batch_size < 1:
            raise ValueError("embed_batch_size must be positive")
        self.chunker = chunker
        self.embedder = embedder
        self.vector_index = vector_index
        self.blob_repo = blob_repo
        self.chunk_repo = chunk_repo
        self.symbol_projection = symbol_projection
        self.embed_batch_size = embed_batch_size
        self.path_store = path_store
        self._embedding_enabled = embedding_enabled
        # None means lexical recall is off: no term index is written or read.
        self.lexical_projection = lexical_projection

    async def ingest(self, blob_name: str, path: str, content: str) -> int:
        """Store metadata and the staged text only; chunking and embedding come later.

        Uploads stay cheap: the caller computes ``blob_name`` from the content
        and polls ``find_missing`` for readiness. Always returns 0.
        """
        existing = await self.blob_repo.get(blob_name)
        is_binary = is_binary_source(content)
        if is_binary or is_ignored_source_path(path):
            if existing is not None and existing.chunks:
                await self.vector_index.delete([blob_name])
                await self.blob_repo.delete(blob_name)
            blob = Blob(
                blob_name=blob_name,
                path=path,
                status=BlobStatus.READY,
                content_size=len(content.encode("utf-8")),
                language=detect_language(path),
                file_type="binary" if is_binary else "ignored",
            )
            await self.blob_repo.save(blob)
            return 0

        if existing is not None and existing.status in (
            BlobStatus.PENDING,
            BlobStatus.READY,
        ):
            await self.blob_repo.touch(blob_name)
            return 0

        blob = Blob(
            blob_name=blob_name,
            path=path,
            status=BlobStatus.PENDING,
            chunks=[],
            content_size=len(content.encode("utf-8")),
            language=detect_language(path),
            file_type="text",
        )
        await self.blob_repo.save(blob)

        # The staged text is what embed_pending chunks; the column is Text, so
        # bytes would be rejected by the driver.
        await self.blob_repo.save_staging(blob_name, content)
        return 0

    async def embed_pending(
        self,
        blob_names: Sequence[str] | None = None,
        *,
        mark_failures: bool = True,
    ) -> int:
        """Run every phase in this pipeline's unit of work; returns the chunks embedded.

        For callers that do not commit between phases (tests, offline tools).
        The application handlers commit after ``prepare`` and run
        ``write_vectors`` outside the transaction.
        """
        batch = await self.prepare(blob_names)
        if batch is None:
            return 0
        try:
            embedded = await self.write_vectors(batch)
        except Exception as exc:
            if mark_failures:
                await self.fail(batch, exc)
            raise
        await self.complete(batch)
        return embedded

    async def prepare(
        self, blob_names: Sequence[str] | None = None
    ) -> PendingBatch | None:
        """Chunk the pending blobs that were only ingested; None when none is pending.

        A blob without chunks is chunked from its staged text, and its symbols
        and terms are projected. A blob whose staged text is gone is marked
        errored here and left out of the batch.
        """
        blobs = await self.blob_repo.find_pending(blob_names)
        if not blobs:
            return None

        for blob in blobs:
            if blob.chunks:
                continue
            content = await self.blob_repo.get_staging(blob.blob_name)
            if content is None:
                if blob.content_size == 0:
                    # An empty file has nothing to chunk; it is marked
                    # ready with the others after the path index.
                    continue
                # The staged text is gone; the blob cannot be indexed.
                blob.mark_error("staging content not found")
                await self.blob_repo.save(blob)
                continue

            chunks = list(self.chunker.chunk(content, blob.path))
            if not chunks:
                # Nothing meaningful to chunk; still indexed by path.
                continue
            await self.chunk_repo.save_many(chunks)
            blob.chunks = [c.to_ref() for c in chunks]
            await self.blob_repo.save(blob)
            # Symbols are extracted once per file and mapped onto the chunks;
            # the term index deduplicates by content hash.
            await self.symbol_projection.index(blob, chunks, content)
            if self.lexical_projection is not None:
                await self.lexical_projection.index(chunks)

        pending = tuple(blob for blob in blobs if blob.status == BlobStatus.PENDING)
        if not pending:
            return None
        unembedded = await self.chunk_repo.find_pending_for_blobs(
            [blob.blob_name for blob in pending]
        )
        return PendingBatch(blobs=pending, chunks=tuple(unembedded))

    async def write_vectors(self, batch: PendingBatch) -> int:
        """Embed the batch and write the vector and path indexes; returns the chunks embedded.

        Touches only the embedder and the vector stores, never the metadata
        repositories, so it runs outside any metadata transaction. Vector
        writes are content addressed: running it twice for the same batch
        writes the same rows.
        """
        if not self._embedding_enabled:
            # Chunks are stored but no vector exists. The blobs stay pending
            # with their staged text and are never marked ready: READY must
            # mean retrievable. This once marked them ready and deleted the
            # staging, which lit up blobs with chunks and no vectors;
            # retrieval returned nothing, and because ingest only touches an
            # existing pending/ready blob, a client re-upload could not repair
            # it. The path index needs the embedder too.
            return 0

        # Embed in pages; the embedder splits a page into provider batches and
        # runs them concurrently. Pages of 64 made synchronous uploads serial
        # at 4.5 s per page (about 14 chunks/s); 256 lets four-way
        # concurrency do its work.
        embedded = 0
        for offset in range(0, len(batch.chunks), self.embed_batch_size):
            chunk_batch = batch.chunks[offset : offset + self.embed_batch_size]
            vectors = await self.embedder.embed_documents(
                [chunk.embedding_text() for chunk in chunk_batch]
            )
            if len(vectors) != len(chunk_batch):
                raise RuntimeError(
                    "Embedding count mismatch: "
                    f"expected {len(chunk_batch)}, got {len(vectors)}"
                )
            await self.vector_index.upsert(
                [
                    VectorRecord(
                        chunk_id=chunk.chunk_id,
                        content_hash=chunk.content_hash,
                        blob_name=chunk.blob_name,
                        path=chunk.path,
                        content=chunk.content,
                        start_line=chunk.start_line,
                        end_line=chunk.end_line,
                        vector=vector,
                        context=chunk.context,
                    )
                    for chunk, vector in zip(chunk_batch, vectors, strict=True)
                ]
            )
            embedded += len(vectors)
        # READY means every artifact the configuration declares is written. A
        # failed path-index write takes the failure/retry path rather than
        # deleting the staging and leaving a silent gap.
        await self._index_paths(batch.blobs)
        return embedded

    async def complete(self, batch: PendingBatch) -> None:
        """Mark the batch's chunks embedded and its still-pending blobs ready."""
        if not self._embedding_enabled:
            return
        await self.chunk_repo.mark_embedded([c.content_hash for c in batch.chunks])
        for blob in await self.blob_repo.find_pending(batch.blob_names):
            blob.mark_ready()
            await self.blob_repo.save(blob)
            await self.blob_repo.delete_staging(blob.blob_name)

    async def fail(self, batch: PendingBatch, error: Exception) -> None:
        """Record ``error`` on the batch's blobs that are still pending."""
        for blob in await self.blob_repo.find_pending(batch.blob_names):
            blob.mark_error(str(error))
            await self.blob_repo.save(blob)

    async def _index_paths(self, blobs: Sequence[Blob]) -> None:
        """Write the path documents before the blobs are marked ready.

        The path index needs only the path text, so it is written in one
        batch here rather than costing ingest an extra embedding call.
        ``is_indexable_path`` excludes dependency, build and binary paths.
        """
        if self.path_store is None:
            return
        indexable = [blob for blob in blobs if is_indexable_path(blob.path)]
        if not indexable:
            return
        docs: list[dict[str, Any]] = [
            {
                "blob_name": blob.blob_name,
                "path": blob.path,
                "path_document": build_path_document(blob.path),
            }
            for blob in indexable
        ]
        vectors = await self.embedder.embed_documents(
            [doc["path_document"] for doc in docs]
        )
        if len(vectors) != len(docs):
            raise RuntimeError(
                "Path embedding count mismatch: "
                f"expected {len(docs)}, got {len(vectors)}"
            )
        for doc, vector in zip(docs, vectors, strict=True):
            doc["path_id"] = f"path_{doc['blob_name']}"
            doc["path_vector"] = vector
        result = await self.path_store.insert(docs)
        logger.info(
            "path index write: {} blobs ({})",
            len(docs),
            result.get("inserted", 0),
        )
