"""Dense index stats are read-only and report Milvus cardinality."""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest
from pymilvus.client.types import LoadState

from oce.infrastructure.milvus3 import Milvus3Client, Milvus3SearchStore
from oce.infrastructure.milvus3.path_index import PathIndexClient
from oce.shared.config.settings import MilvusSettings


@patch("oce.infrastructure.milvus3.client.AsyncMilvusClient")
async def test_read_collection_stats_does_not_initialize_or_create(
    mock_milvus_client_class,
):
    settings = MilvusSettings(
        endpoint="http://localhost:19530",
        collection_name="test_collection",
    )
    mock_client = Mock()
    mock_client.has_collection = AsyncMock(return_value=True)
    mock_client.get_collection_stats = AsyncMock(return_value={"row_count": "42"})
    mock_milvus_client_class.return_value = mock_client
    client = Milvus3Client(settings)

    exists, entities = await client.read_collection_stats()

    assert (exists, entities) == (True, 42)
    mock_client.create_collection.assert_not_called()
    mock_client.load_collection.assert_not_called()


@patch("oce.infrastructure.milvus3.search_store.Milvus3Client")
async def test_index_stats_report_collection_cardinality(mock_client_class):
    settings = MilvusSettings(
        endpoint="http://localhost:19530",
        collection_name="test_collection",
    )
    mock_client = mock_client_class.return_value
    mock_client.read_collection_stats = AsyncMock(return_value=(True, 12))
    store = Milvus3SearchStore(settings)

    stats = await store.index_stats()

    assert stats.available is True
    assert stats.collection_name == settings.collection_name
    assert stats.entities == 12


@patch("oce.infrastructure.milvus3.search_store.Milvus3Client")
async def test_dense_data_probe_uses_collection_cardinality(mock_client_class):
    mock_client = mock_client_class.return_value
    mock_client.read_collection_stats = AsyncMock(return_value=(True, 3))
    store = Milvus3SearchStore(MilvusSettings())

    assert await store.has_index_data() is True


@patch("oce.infrastructure.milvus3.path_index.AsyncMilvusClient")
async def test_uninitialized_path_stats_do_not_create_collection(client_class):
    settings = MilvusSettings(
        endpoint="http://localhost:19530",
        path_collection_name="test_paths",
    )
    client = PathIndexClient(settings)

    stats = await client.index_stats()

    assert stats.enabled is True
    assert stats.available is False
    assert stats.collection_name == "test_paths"
    assert stats.error_type == "NotInitialized"
    assert client._initialized is False
    client_class.return_value.create_collection.assert_not_called()
    client_class.return_value.load_collection.assert_not_called()


@patch("oce.infrastructure.milvus3.path_index.AsyncMilvusClient")
async def test_path_data_probe_does_not_create_collection(client_class):
    milvus = client_class.return_value
    milvus.has_collection = AsyncMock(return_value=True)
    milvus.get_collection_stats = AsyncMock(return_value={"row_count": "2"})
    client = PathIndexClient(
        MilvusSettings(
            endpoint="http://localhost:19530",
            path_collection_name="test_paths",
        )
    )
    assert await client.has_index_data() is True
    milvus.has_collection.assert_awaited_once_with("test_paths")
    milvus.get_collection_stats.assert_awaited_once_with("test_paths")
    milvus.create_collection.assert_not_called()
    milvus.load_collection.assert_not_called()


async def test_path_filters_reject_non_sha256_values_before_connecting():
    client = PathIndexClient(
        MilvusSettings(
            endpoint="http://localhost:19530",
            path_collection_name="test_paths",
        )
    )

    with pytest.raises(ValueError, match="SHA256"):
        await client.search_paths(
            [0.1] * client.dense_dim,
            allowed_blob_names=['x" or true'],
        )
    with pytest.raises(ValueError, match="SHA256"):
        await client.delete_by_blob_names(['x" or true'])

    assert client._initialized is False


@patch("oce.infrastructure.milvus3.path_index.AsyncMilvusClient")
async def test_path_filters_use_validated_blob_names(client_class):
    milvus = client_class.return_value
    milvus.search = AsyncMock(return_value=[])
    milvus.delete = AsyncMock(return_value={})
    client = PathIndexClient(
        MilvusSettings(
            endpoint="http://localhost:19530",
            path_collection_name="test_paths",
        )
    )
    client._initialized = True
    blob_name = "a" * 64

    await client.search_paths(
        [0.1] * client.dense_dim,
        allowed_blob_names=[blob_name],
    )
    await client.delete_by_blob_names([blob_name])

    assert milvus.search.await_args.kwargs["filter"] == (
        f'blob_name in ["{blob_name}"]'
    )
    assert milvus.delete.await_args.kwargs["filter"] == (
        f'blob_name in ["{blob_name}"]'
    )


@patch("oce.infrastructure.milvus3.path_index.AsyncMilvusClient")
async def test_concurrent_path_initialization_runs_once(_client_class):
    client = PathIndexClient(MilvusSettings())
    client._ensure_collection = AsyncMock()

    await asyncio.gather(client.initialize(), client.initialize())

    client._ensure_collection.assert_awaited_once_with()
    assert client._initialized is True


@patch("oce.infrastructure.milvus3.path_index.AsyncMilvusClient")
async def test_existing_path_collection_is_loaded_without_recreation(client_class):
    milvus = client_class.return_value
    milvus.has_collection = AsyncMock(return_value=True)
    milvus.list_indexes = AsyncMock(return_value=["path_vector"])
    milvus.get_load_state = AsyncMock(return_value={"state": LoadState.NotLoad})
    milvus.load_collection = AsyncMock()
    client = PathIndexClient(MilvusSettings())

    await client.initialize()

    milvus.load_collection.assert_awaited_once_with(client.collection_name)
    milvus.create_collection.assert_not_called()
    milvus.create_index.assert_not_called()


@patch("oce.infrastructure.milvus3.path_index.AsyncMilvusClient")
async def test_missing_path_collection_creates_schema_and_index(client_class):
    milvus = client_class.return_value
    milvus.has_collection = AsyncMock(return_value=False)
    milvus.create_collection = AsyncMock()
    milvus.list_indexes = AsyncMock(return_value=[])
    milvus.create_index = AsyncMock()
    milvus.get_load_state = AsyncMock(return_value={"state": LoadState.NotLoad})
    milvus.load_collection = AsyncMock()
    index_params = Mock()
    milvus.prepare_index_params.return_value = index_params
    client = PathIndexClient(MilvusSettings())

    await client.initialize()

    assert milvus.create_collection.await_args.kwargs["collection_name"] == (
        client.collection_name
    )
    index_params.add_index.assert_called_once()
    milvus.create_index.assert_awaited_once_with(
        collection_name=client.collection_name,
        index_params=index_params,
    )
    milvus.load_collection.assert_awaited_once_with(client.collection_name)


@patch("oce.infrastructure.milvus3.path_index.AsyncMilvusClient")
async def test_path_close_releases_owned_connection(client_class):
    milvus = client_class.return_value
    milvus.close = AsyncMock()
    client = PathIndexClient(MilvusSettings())
    client._initialized = True

    await client.close()

    milvus.close.assert_awaited_once_with()
    assert client._initialized is False
    assert client._closed is True

    await client.close()
    milvus.close.assert_awaited_once_with()
    with pytest.raises(RuntimeError, match="closed"):
        await client.initialize()


@patch("oce.infrastructure.milvus3.path_index.MilvusClient")
async def test_local_path_search_offloads_sync_client(client_class):
    milvus = client_class.return_value
    milvus.search.return_value = []
    client = PathIndexClient(MilvusSettings(endpoint="/tmp/oce-path-test.db"))
    client._initialized = True
    blob_name = "a" * 64

    with patch(
        "oce.infrastructure.milvus3.path_index.asyncio.to_thread",
        new_callable=AsyncMock,
    ) as to_thread:
        to_thread.side_effect = lambda func, *args, **kwargs: func(*args, **kwargs)
        await client.search_paths(
            [0.1] * client.dense_dim,
            allowed_blob_names=[blob_name],
        )

    to_thread.assert_awaited_once()
    milvus.search.assert_called_once()
