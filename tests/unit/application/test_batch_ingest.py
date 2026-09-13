"""Tests for transaction-batched ingestion."""

from __future__ import annotations

from oce.application.commands.ingest import (
    BlobIngest,
    IngestBlobsCommand,
    IngestBlobsCommandHandler,
    build_pipeline_factory,
)
from oce.domain.chunk import RecursiveChunker
from tests.fakes.indexing import ConstantEmbedder
from tests.fakes.retrieval import FakeSearchStore
from tests.unit.application.fakes import (
    FakeUnitOfWorkFactory,
    blob_name,
)


async def test_ingest_blobs_commits_one_transaction_for_the_batch():
    """A batch ingest commits in one transaction."""
    factory = FakeUnitOfWorkFactory()
    handler = IngestBlobsCommandHandler(
        factory,
        build_pipeline_factory(
            chunker=RecursiveChunker(chunk_size=6000, chunk_overlap=200),
            embedder=ConstantEmbedder(),
            vector_index=FakeSearchStore(),
        ),
    )
    blobs = tuple(
        BlobIngest(
            blob_name(f"src/{index}.py", f"print({index})"),
            f"src/{index}.py",
            f"print({index})",
        )
        for index in range(3)
    )

    await handler.handle(IngestBlobsCommand(blobs))

    assert factory.uow.commits == 1
    # The staged text was stored.
    for blob in blobs:
        assert factory.uow.blobs.staging.get(blob.blob_name) == blob.content
