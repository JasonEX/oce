"""IndexingPipeline over in-memory repositories: chunking, lazy embedding, state transitions."""

from __future__ import annotations

import hashlib

import pytest

from oce.domain.blob.blob import BlobStatus
from oce.domain.chunk import Chunk, LocatedChunk, RecursiveChunker
from oce.domain.services.indexing import IndexingPipeline
from tests.fakes.indexing import (
    ConstantEmbedder,
    FakeLexicalProjection,
    FakeSymbolProjection,
    RecordingVectorIndex,
)


class FakeBlobRepo:
    """In-memory BlobRepository with only what the pipeline calls."""

    def __init__(self):
        self.blobs: dict[str, object] = {}
        self.staging: dict[str, bytes] = {}

    async def get(self, blob_name: str):
        return self.blobs.get(blob_name)

    async def save(self, blob) -> None:
        self.blobs[blob.blob_name] = blob

    async def get_staging(self, blob_name: str) -> str | None:
        return self.staging.get(blob_name)

    async def save_staging(self, blob_name: str, content: str) -> None:
        # blob_staging.content is a Text column; assert str so bytes never return.
        assert isinstance(content, str), "staged text must be str"
        self.staging[blob_name] = content

    async def delete_staging(self, blob_name: str) -> None:
        self.staging.pop(blob_name, None)

    async def find_pending(self, blob_names=None) -> list:
        names = set(blob_names) if blob_names is not None else None
        return [
            b
            for b in self.blobs.values()
            if b.status == BlobStatus.PENDING
            and (names is None or b.blob_name in names)
        ]


class FakeChunkRepo:
    """In-memory ChunkRepository."""

    def __init__(self):
        self.chunks: dict[str, object] = {}
        self.pending: list[LocatedChunk] = []
        self.last_blob_name: str = ""

    async def save_many(self, chunks) -> None:
        for c in chunks:
            self.chunks[c.content_hash] = c
            # Also pending, as a freshly saved chunk is; blob_name is derived
            # from the path for simplicity.
            blob_name = hashlib.sha256(c.path.encode()).hexdigest()
            located = LocatedChunk(
                blob_name=blob_name,
                content_hash=c.content_hash,
                path=c.path,
                content=c.content,
                start_line=c.start_line,
                end_line=c.end_line,
                context=c.context,
            )
            self.pending.append(located)

    async def find_pending_for_blobs(
        self, blob_names, limit=None
    ) -> list[LocatedChunk]:
        # Every pending chunk; the real store filters by blob_names.
        result = list(self.pending)
        if limit:
            result = result[:limit]
        return result

    async def mark_embedded(self, content_hashes: list[str]) -> None:
        # Embedded chunks leave the pending list.
        self.pending = [c for c in self.pending if c.content_hash not in content_hashes]


class RecordingPathStore:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.documents: list[dict] = []

    async def insert(self, path_docs):
        if self.error is not None:
            raise self.error
        self.documents.extend(path_docs)
        return {"inserted": len(path_docs)}


def _blob_name(path: str, content: str) -> str:
    return hashlib.sha256((path + content).encode("utf-8")).hexdigest()


@pytest.fixture
def indexing_pipeline():
    blob_repo = FakeBlobRepo()
    chunk_repo = FakeChunkRepo()
    embedder = ConstantEmbedder()

    return IndexingPipeline(
        chunker=RecursiveChunker(chunk_size=6000, chunk_overlap=200),
        embedder=embedder,
        vector_index=RecordingVectorIndex(),
        blob_repo=blob_repo,
        chunk_repo=chunk_repo,
        symbol_projection=FakeSymbolProjection(),
    )


class TestIngest:
    async def test_ingest_saves_blob_and_chunks(self, indexing_pipeline):
        # 100 lines of about 10 characters; a small chunker yields several chunks.
        content = "\n".join(f"line{i}" for i in range(100))
        name = _blob_name("src/a.py", content)

        # A small chunker to exercise splitting.
        original_chunker = indexing_pipeline.chunker
        indexing_pipeline.chunker = RecursiveChunker(chunk_size=400, chunk_overlap=50)

        count = await indexing_pipeline.ingest(name, "src/a.py", content)

        # ingest returns 0; embed_pending chunks.
        assert count == 0

        embedded_count = await indexing_pipeline.embed_pending([name])
        assert embedded_count >= 2  # 1000 characters at 400 per chunk

        blob = indexing_pipeline.blob_repo.blobs[name]
        assert blob.status == BlobStatus.READY
        assert len(blob.chunks) == embedded_count

        indexing_pipeline.chunker = original_chunker

    async def test_ingest_empty_content_keeps_blob(self, indexing_pipeline):
        name = _blob_name("src/empty.py", "")
        count = await indexing_pipeline.ingest(name, "src/empty.py", "")

        assert count == 0

        # An empty file is pending until embed_pending handles it.
        blob = indexing_pipeline.blob_repo.blobs[name]
        assert blob.status == BlobStatus.PENDING

        # embed_pending marks empty content ready.
        await indexing_pipeline.embed_pending([name])
        blob = indexing_pipeline.blob_repo.blobs[name]
        assert blob.status == BlobStatus.READY
        assert blob.chunks == []

    async def test_ingest_ignored_source_never_reaches_chunker(self, indexing_pipeline):
        class FailingChunker:
            def chunk(self, _content, _path):
                raise AssertionError("ignored source reached chunker")

        indexing_pipeline.chunker = FailingChunker()
        content = '<svg><path d="M0 0" /></svg>'
        name = _blob_name("assets/logo.svg", content)

        count = await indexing_pipeline.ingest(name, "assets/logo.svg", content)

        assert count == 0
        blob = indexing_pipeline.blob_repo.blobs[name]
        assert blob.status == BlobStatus.READY
        assert blob.file_type == "ignored"

    async def test_ingest_binary_source_never_reaches_chunker(self, indexing_pipeline):
        class FailingChunker:
            def chunk(self, _content, _path):
                raise AssertionError("binary source reached chunker")

        indexing_pipeline.chunker = FailingChunker()
        content = "PNG\x00data"
        name = _blob_name("assets/logo.dat", content)

        count = await indexing_pipeline.ingest(name, "assets/logo.dat", content)

        assert count == 0
        assert indexing_pipeline.blob_repo.blobs[name].file_type == "binary"

    async def test_ingest_same_blob_is_idempotent(self, indexing_pipeline):
        content = "print('same')\n"
        name = _blob_name("src/same.py", content)

        first = await indexing_pipeline.ingest(name, "src/same.py", content)
        second = await indexing_pipeline.ingest(name, "src/same.py", content)

        assert first == second
        assert indexing_pipeline.blob_repo.staging[name] == content


class TestEmbedPending:
    async def test_embed_pending_marks_blob_ready(self, indexing_pipeline):
        # Reset the fixture state completely.
        indexing_pipeline.chunk_repo.pending.clear()
        indexing_pipeline.chunk_repo.chunks.clear()
        indexing_pipeline.vector_index.records.clear()
        indexing_pipeline.blob_repo.blobs.clear()
        indexing_pipeline.blob_repo.staging.clear()

        content = "print('hello')\n"
        name = _blob_name("src/hello.py", content)

        # A chunk that is stored but not embedded.
        chunk = Chunk(
            content_hash=Chunk.compute_hash(content),
            path="src/hello.py",
            content=content,
            start_line=1,
            end_line=1,
        )

        # A pending blob that already has chunks.
        from oce.domain.blob.blob import Blob, BlobStatus

        blob = Blob(
            blob_name=name,
            path="src/hello.py",
            status=BlobStatus.PENDING,
            chunks=[chunk.to_ref()],  # chunked, not yet embedded
            content_size=len(content),
            language="python",
            file_type="text",
        )
        await indexing_pipeline.blob_repo.save(blob)

        # Pending, waiting to be embedded.
        indexing_pipeline.chunk_repo.pending = [
            LocatedChunk(name, chunk.content_hash, chunk.path, chunk.content, 1, 1)
        ]

        embedded = await indexing_pipeline.embed_pending([name])

        assert embedded == 1
        blob = indexing_pipeline.blob_repo.blobs[name]
        assert blob.status == BlobStatus.READY
        # The chunk was embedded.
        assert len(indexing_pipeline.vector_index.records) == 1
        assert (
            indexing_pipeline.vector_index.records[0].content_hash == chunk.content_hash
        )

    async def test_path_index_is_written_before_blob_becomes_ready(
        self, indexing_pipeline
    ):
        content = "def feature():\n    return True\n"
        name = _blob_name("src/feature.py", content)
        path_store = RecordingPathStore()
        indexing_pipeline.path_store = path_store

        await indexing_pipeline.ingest(name, "src/feature.py", content)
        await indexing_pipeline.embed_pending([name])

        assert indexing_pipeline.blob_repo.blobs[name].status == BlobStatus.READY
        assert len(path_store.documents) == 1
        assert path_store.documents[0]["blob_name"] == name
        assert path_store.documents[0]["path"] == "src/feature.py"
        assert path_store.documents[0]["path_vector"] == [1.0] * 4

    async def test_path_index_failure_keeps_blob_retryable(self, indexing_pipeline):
        content = "def feature():\n    return True\n"
        name = _blob_name("src/feature.py", content)
        indexing_pipeline.path_store = RecordingPathStore(
            error=RuntimeError("path store unavailable")
        )

        await indexing_pipeline.ingest(name, "src/feature.py", content)
        with pytest.raises(RuntimeError, match="path store unavailable"):
            await indexing_pipeline.embed_pending([name], mark_failures=False)

        blob = indexing_pipeline.blob_repo.blobs[name]
        assert blob.status == BlobStatus.PENDING
        assert name in indexing_pipeline.blob_repo.staging

    async def test_embed_pending_disabled_keeps_pending_and_staging(
        self, indexing_pipeline
    ):
        """Regression: with embedding disabled a chunked blob stays pending with its staging.

        The production failure: chunks stored, no vectors in Milvus, blob
        marked READY, every retrieval empty, and content addressing made a
        client re-upload a no-op. Now the blob stays pending with its text
        and is embedded once re-enqueued with embedding enabled.
        """
        from oce.domain.blob.blob import Blob, BlobStatus

        indexing_pipeline.chunk_repo.pending.clear()
        indexing_pipeline.chunk_repo.chunks.clear()
        indexing_pipeline.vector_index.records.clear()
        indexing_pipeline.blob_repo.blobs.clear()
        indexing_pipeline.blob_repo.staging.clear()

        content = "print('hello')\n"
        name = _blob_name("src/hello.py", content)
        chunk = Chunk(
            content_hash=Chunk.compute_hash(content),
            path="src/hello.py",
            content=content,
            start_line=1,
            end_line=1,
        )
        blob = Blob(
            blob_name=name,
            path="src/hello.py",
            status=BlobStatus.PENDING,
            chunks=[chunk.to_ref()],  # chunked, not yet embedded
            content_size=len(content),
            language="python",
            file_type="text",
        )
        await indexing_pipeline.blob_repo.save(blob)
        await indexing_pipeline.blob_repo.save_staging(name, content)
        indexing_pipeline.chunk_repo.pending = [
            LocatedChunk(name, chunk.content_hash, chunk.path, chunk.content, 1, 1)
        ]

        disabled_pipeline = IndexingPipeline(
            chunker=indexing_pipeline.chunker,
            embedder=indexing_pipeline.embedder,
            vector_index=indexing_pipeline.vector_index,
            blob_repo=indexing_pipeline.blob_repo,
            chunk_repo=indexing_pipeline.chunk_repo,
            symbol_projection=indexing_pipeline.symbol_projection,
            path_store=indexing_pipeline.path_store,
            embedding_enabled=False,
        )
        embedded = await disabled_pipeline.embed_pending([name])

        assert embedded == 0
        assert indexing_pipeline.vector_index.records == []  # no vector was written
        blob = indexing_pipeline.blob_repo.blobs[name]
        assert blob.status == BlobStatus.PENDING  # never a false READY
        assert (
            name in indexing_pipeline.blob_repo.staging
        )  # the staged text is kept for later embedding
        assert (
            len(indexing_pipeline.chunk_repo.pending) == 1
        )  # the chunk was not consumed

    async def test_embed_pending_no_pending_returns_zero(self, indexing_pipeline):
        name = _blob_name("src/x.py", "print(1)\n")
        await indexing_pipeline.ingest(name, "src/x.py", "print(1)\n")

        # Clear pending and mark the blob ready, as after a completed embedding.
        indexing_pipeline.chunk_repo.pending.clear()
        blob = indexing_pipeline.blob_repo.blobs[name]
        blob.mark_ready()
        await indexing_pipeline.blob_repo.save(blob)

        assert await indexing_pipeline.embed_pending([name]) == 0

    async def test_embed_failure_marks_blob_error(self, indexing_pipeline):
        class FailingEmbedder:
            async def embed_documents(self, _texts):
                raise RuntimeError("provider rejected input")

        content = "print('broken')\n"
        name = _blob_name("src/broken.py", content)
        await indexing_pipeline.ingest(name, "src/broken.py", content)
        chunk = Chunk(
            content_hash=Chunk.compute_hash(content),
            path="src/broken.py",
            content=content,
            start_line=1,
            end_line=1,
        )
        indexing_pipeline.chunk_repo.pending = [
            LocatedChunk(name, chunk.content_hash, chunk.path, chunk.content, 1, 1)
        ]
        indexing_pipeline.embedder = FailingEmbedder()

        with pytest.raises(RuntimeError, match="provider rejected input"):
            await indexing_pipeline.embed_pending([name])

        blob = indexing_pipeline.blob_repo.blobs[name]
        assert blob.status == BlobStatus.ERROR
        assert blob.error_message == "provider rejected input"


class TestProjections:
    async def test_lexical_projection_and_context_reach_the_vector_index(self):
        blob_repo = FakeBlobRepo()
        chunk_repo = FakeChunkRepo()
        vector_index = RecordingVectorIndex()
        lexical = FakeLexicalProjection()
        symbols = FakeSymbolProjection()

        class ContextChunker:
            def chunk(self, content, path):
                return [
                    Chunk(
                        Chunk.compute_hash(content),
                        path,
                        content,
                        1,
                        content.count("\n") + 1,
                        context="class Svc:",
                    )
                ]

        pipeline = IndexingPipeline(
            chunker=ContextChunker(),
            embedder=ConstantEmbedder(),
            vector_index=vector_index,
            blob_repo=blob_repo,
            chunk_repo=chunk_repo,
            symbol_projection=symbols,
            lexical_projection=lexical,
        )
        content = "def run(self):\n    return 1"
        name = _blob_name("src/svc.py", content)
        await pipeline.ingest(name, "src/svc.py", content)
        await pipeline.embed_pending([name])

        assert lexical.indexed == [(Chunk.compute_hash(content),)]
        assert symbols.indexed[0][0] == name
        assert [record.context for record in vector_index.records] == ["class Svc:"]
        assert blob_repo.blobs[name].chunks[0].context == "class Svc:"
