"""Tests for cross-handler application orchestration."""

from oce.application.commands.checkpoint import CheckpointCommand
from oce.application.commands.ingest import IngestBlobsCommand
from oce.application.queries.search import SearchQuery, SearchResult
from oce.application.queries.status import ResolveScopeQuery, ResolveScopeResult
from oce.application.service import BlobUpload, RetrievalApplication, compute_blob_name
from oce.domain.services.search import SearchScope
from tests.fakes.application import mocked_use_cases


async def test_retrieve_passes_deleted_blobs_to_scope_without_delete_side_effect():
    commands, queries, credentials = mocked_use_cases()
    application = RetrievalApplication(commands, queries, credentials=credentials)

    result = await application.retrieve(
        "entry point",
        checkpoint_id="chain:1",
        added_blobs=["blob-a"],
        deleted_blobs=["blob-b"],
    )

    commands.gc.handle.assert_not_awaited()
    queries.resolve_scope.handle.assert_awaited_once_with(
        ResolveScopeQuery("chain:1", ("blob-a",), ("blob-b",))
    )
    assert result.hits


async def test_retrieve_with_empty_scope_requests_empty_search():
    commands, queries, credentials = mocked_use_cases()
    queries.resolve_scope.handle.return_value = ResolveScopeResult(
        SearchScope(frozenset())
    )
    queries.search.handle.return_value = SearchResult([])
    application = RetrievalApplication(commands, queries, credentials=credentials)

    result = await application.retrieve("entry point")

    queries.search.handle.assert_awaited_once_with(
        SearchQuery("entry point", SearchScope(frozenset()), source="retrieval")
    )
    assert result.hits == ()


async def test_batch_upload_does_not_embed_synchronously_with_background_worker():
    commands, queries, credentials = mocked_use_cases()
    application = RetrievalApplication(
        commands, queries, credentials=credentials, background_indexing=True
    )

    result = await application.batch_upload([])

    assert result.embedded_count == 0
    commands.ingest.handle.assert_awaited_once_with(IngestBlobsCommand(()))
    commands.embed_pending.handle.assert_not_awaited()


async def test_batch_upload_registers_blobs_to_checkpoint_when_id_given():
    commands, queries, credentials = mocked_use_cases()
    application = RetrievalApplication(commands, queries, credentials=credentials)
    blob = BlobUpload("src/a.py", "print(1)\n")

    await application.batch_upload([blob], checkpoint_id="chain:2")

    commands.checkpoint.handle.assert_awaited_once_with(
        CheckpointCommand("chain:2", (compute_blob_name(blob.path, blob.content),), ())
    )


async def test_batch_upload_without_checkpoint_id_skips_checkpoint():
    commands, queries, credentials = mocked_use_cases()
    application = RetrievalApplication(commands, queries, credentials=credentials)

    await application.batch_upload([BlobUpload("src/a.py", "print(1)\n")])

    commands.checkpoint.handle.assert_not_awaited()
