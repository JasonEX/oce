"""Dense index stats are read-only and report Milvus cardinality."""

from unittest.mock import AsyncMock, Mock, patch

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


async def test_uninitialized_path_stats_do_not_create_collection():
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
    assert client.collection is None
