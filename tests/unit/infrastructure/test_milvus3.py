"""Milvus 3.0 内容 collection：客户端、Schema 与 SearchStore。"""

from unittest.mock import AsyncMock, Mock, patch

import pytest
from pymilvus import CollectionSchema
from pymilvus.client.types import LoadState

from oce.domain.services.search import VectorRecord
from oce.infrastructure.milvus3.base import build_blob_filter
from oce.infrastructure.milvus3.client import Milvus3Client
from oce.infrastructure.milvus3.schema import create_oce_collection_schema
from oce.infrastructure.milvus3.search_store import Milvus3SearchStore
from oce.shared.config.settings import MilvusSettings

DIM = 1024


def _record(index: int, content: str = "def main():", blob_name: str = "a" * 64):
    return VectorRecord(
        chunk_id=f"chunk{index}",
        content_hash=f"hash{index}",
        blob_name=blob_name,
        path="src/main.py",
        content=content,
        start_line=index,
        end_line=index,
        vector=[0.1 * index] * DIM,
    )


def _loaded_remote_client() -> Mock:
    client = Mock()
    client.has_collection = AsyncMock(return_value=True)
    client.list_indexes = AsyncMock(return_value=["dense_vector"])
    client.get_load_state = AsyncMock(return_value={"state": LoadState.Loaded})
    return client


def test_build_blob_filter_reports_exact_production_expression_size():
    names = ["a" * 64, "b" * 64]

    expression = build_blob_filter(names)

    assert expression == f'blob_name in ["{names[0]}", "{names[1]}"]'
    assert build_blob_filter([]) is None


class TestMilvusSchema:
    def test_create_schema_default_dim(self):
        schema = create_oce_collection_schema()

        assert isinstance(schema, CollectionSchema)
        assert [f.name for f in schema.fields] == [
            "chunk_id",
            "content_hash",
            "content",
            "dense_vector",
            "blob_name",
            "metadata",
        ]
        assert len(schema.functions) == 0

    def test_create_schema_custom_dim(self):
        schema = create_oce_collection_schema(dense_dim=768)

        dense_field = next(f for f in schema.fields if f.name == "dense_vector")
        assert dense_field.params["dim"] == 768


class TestMilvus3Client:
    @pytest.fixture
    def settings(self):
        return MilvusSettings(
            endpoint="http://localhost:19530",
            token=None,
            collection_name="test_collection",
        )

    @patch("oce.infrastructure.milvus3.base.AsyncMilvusClient")
    async def test_client_init_creates_collection(self, client_class, settings):
        mock_client = Mock()
        mock_client.has_collection = AsyncMock(return_value=False)
        mock_client.create_collection = AsyncMock()
        mock_client.list_indexes = AsyncMock(return_value=[])
        mock_client.create_index = AsyncMock()
        mock_client.get_load_state = AsyncMock(
            return_value={"state": LoadState.NotLoad}
        )
        mock_client.load_collection = AsyncMock()
        mock_client.prepare_index_params = Mock(return_value=Mock())
        client_class.return_value = mock_client

        client = Milvus3Client(settings, dense_dim=DIM)
        await client.initialize()

        client_class.assert_called_once_with(
            uri=settings.endpoint, token=settings.token
        )
        schema = mock_client.create_collection.await_args.kwargs["schema"]
        dense_field = next(f for f in schema.fields if f.name == "dense_vector")
        assert dense_field.params["dim"] == DIM
        mock_client.create_index.assert_awaited_once()
        mock_client.load_collection.assert_awaited_once_with(settings.collection_name)

    @patch("oce.infrastructure.milvus3.base.MilvusClient")
    async def test_local_uri_uses_sync_client_in_worker_thread(self, client_class):
        settings = MilvusSettings(endpoint="./oce_milvus.db", collection_name="c")
        mock_client = Mock()
        mock_client.has_collection.return_value = True
        mock_client.list_indexes.return_value = ["dense_vector"]
        mock_client.get_load_state.return_value = {"state": LoadState.Loaded}
        client_class.return_value = mock_client

        client = Milvus3Client(settings, dense_dim=DIM)
        await client.initialize()

        client_class.assert_called_once_with(
            uri=settings.endpoint, token=settings.token
        )
        mock_client.has_collection.assert_called_once_with("c")
        mock_client.get_load_state.assert_called_once_with("c")

    @patch("oce.infrastructure.milvus3.base.AsyncMilvusClient")
    async def test_client_init_loads_existing_collection(self, client_class, settings):
        mock_client = _loaded_remote_client()
        mock_client.get_load_state = AsyncMock(
            return_value={"state": LoadState.NotLoad}
        )
        mock_client.load_collection = AsyncMock()
        client_class.return_value = mock_client

        client = Milvus3Client(settings, dense_dim=DIM)
        await client.initialize()

        mock_client.load_collection.assert_awaited_once_with(settings.collection_name)
        mock_client.create_collection.assert_not_called()

    @patch("oce.infrastructure.milvus3.base.AsyncMilvusClient")
    async def test_insert_chunks(self, client_class, settings):
        mock_client = _loaded_remote_client()
        mock_client.upsert = AsyncMock(return_value={"upsert_count": 2})
        client_class.return_value = mock_client
        client = Milvus3Client(settings, dense_dim=DIM)

        inserted = await client.insert([_record(1), _record(2, "print('hello')")])

        assert inserted == 2
        rows = mock_client.upsert.await_args.kwargs["data"]
        assert [row["chunk_id"] for row in rows] == ["chunk1", "chunk2"]
        assert rows[0]["dense_vector"] == [0.1] * DIM
        assert rows[1]["metadata"] == {
            "path": "src/main.py",
            "start_line": 2,
            "end_line": 2,
        }

    @patch("oce.infrastructure.milvus3.base.AsyncMilvusClient")
    async def test_insert_limits_content_by_utf8_bytes(self, client_class, settings):
        mock_client = _loaded_remote_client()
        mock_client.upsert = AsyncMock(return_value={"upsert_count": 1})
        client_class.return_value = mock_client
        client = Milvus3Client(settings, dense_dim=DIM)
        original = "界" * 30_000

        await client.insert([_record(1, original)])

        row = mock_client.upsert.await_args.kwargs["data"][0]
        assert len(row["content"].encode("utf-8")) <= 65_535
        assert row["content"].encode("utf-8").decode("utf-8") == row["content"]
        assert row["metadata"]["content_truncated"] is True
        assert row["metadata"]["content_bytes"] == len(original.encode("utf-8"))

    @patch("oce.infrastructure.milvus3.base.AsyncMilvusClient")
    async def test_dense_search(self, client_class, settings):
        mock_client = _loaded_remote_client()
        hit = Mock()
        hit.entity = {
            "content_hash": "hash1",
            "content": "def main():",
            "blob_name": "a" * 64,
            "metadata": {"path": "src/main.py", "start_line": 3, "end_line": 4},
        }
        hit.distance = 0.9
        mock_client.search = AsyncMock(return_value=[[hit]])
        client_class.return_value = mock_client
        client = Milvus3Client(settings, dense_dim=DIM)

        results = await client.search([0.1] * DIM, blob_filter=["a" * 64], top_k=10)

        assert len(results) == 1
        assert results[0].content_hash == "hash1"
        assert results[0].score == 0.9
        assert (results[0].path, results[0].start_line, results[0].end_line) == (
            "src/main.py",
            3,
            4,
        )
        search_kwargs = mock_client.search.await_args.kwargs
        assert search_kwargs["limit"] == 10
        assert search_kwargs["search_params"]["params"]["ef"] == settings.hnsw_ef_search
        assert search_kwargs["filter"] == f'blob_name in ["{"a" * 64}"]'

    @patch("oce.infrastructure.milvus3.base.MilvusClient")
    async def test_local_search_parses_dict_hits(self, client_class):
        settings = MilvusSettings(endpoint="./oce_milvus.db", collection_name="c")
        mock_client = Mock()
        mock_client.has_collection.return_value = True
        mock_client.list_indexes.return_value = ["dense_vector"]
        mock_client.get_load_state.return_value = {"state": LoadState.Loaded}
        mock_client.search.return_value = [
            [
                {
                    "entity": {
                        "content_hash": "hash1",
                        "content": "def main():",
                        "blob_name": "a" * 64,
                        "metadata": {"path": "src/main.py"},
                    },
                    "distance": 0.9,
                }
            ]
        ]
        client_class.return_value = mock_client
        client = Milvus3Client(settings, dense_dim=DIM)

        results = await client.search([0.1] * DIM, top_k=1)

        assert results[0].content_hash == "hash1"
        assert results[0].score == 0.9

    @patch("oce.infrastructure.milvus3.base.AsyncMilvusClient")
    async def test_dense_search_raises_ef_to_candidate_limit(
        self, client_class, settings
    ):
        mock_client = _loaded_remote_client()
        mock_client.search = AsyncMock(return_value=[[]])
        client_class.return_value = mock_client
        client = Milvus3Client(settings, dense_dim=DIM)

        await client.search([0.1] * DIM, top_k=50)

        search_kwargs = mock_client.search.await_args.kwargs
        assert search_kwargs["limit"] == 50
        assert search_kwargs["search_params"]["params"]["ef"] == max(
            settings.hnsw_ef_search, 100
        )

    @patch("oce.infrastructure.milvus3.base.AsyncMilvusClient")
    async def test_blob_filters_reject_non_sha256_values(self, client_class, settings):
        client_class.return_value = Mock()
        client = Milvus3Client(settings, dense_dim=DIM)

        with pytest.raises(ValueError, match="SHA256"):
            await client.search([0.1] * DIM, blob_filter=['x" or true'])
        with pytest.raises(ValueError, match="SHA256"):
            await client.delete_by_blob_names(['x" or true'])
        client_class.return_value.search.assert_not_called()

    @patch("oce.infrastructure.milvus3.base.AsyncMilvusClient")
    async def test_delete_uses_validated_filter(self, client_class, settings):
        mock_client = _loaded_remote_client()
        mock_client.delete = AsyncMock(return_value=["chunk1", "chunk2"])
        client_class.return_value = mock_client
        client = Milvus3Client(settings, dense_dim=DIM)
        blob_name = "a" * 64

        await client.delete_by_blob_names([blob_name])

        mock_client.delete.assert_awaited_once_with(
            collection_name=settings.collection_name,
            filter=f'blob_name in ["{blob_name}"]',
        )


class TestMilvus3SearchStore:
    @pytest.fixture
    def settings(self):
        return MilvusSettings(endpoint="http://localhost:19530", collection_name="c")

    @patch("oce.infrastructure.milvus3.search_store.Milvus3Client")
    def test_search_store_init(self, client_class, settings):
        Milvus3SearchStore(settings, dense_dim=DIM)

        client_class.assert_called_once_with(settings, dense_dim=DIM)

    @patch("oce.infrastructure.milvus3.search_store.Milvus3Client")
    async def test_search_passes_scope_and_applies_threshold(
        self, client_class, settings
    ):
        mock_client = client_class.return_value
        strong = _record(1)
        mock_client.search = AsyncMock(
            return_value=[
                _hit(strong, 0.9),
                _hit(_record(2, blob_name="b" * 64), 0.1),
            ]
        )
        store = Milvus3SearchStore(settings, dense_dim=DIM)

        hits = await store.search(
            query_vector=[0.1] * DIM,
            allowed_blob_names=["a" * 64],
            top_k=5,
            vector_threshold=0.5,
        )

        assert [hit.content_hash for hit in hits] == ["hash1"]
        assert mock_client.search.await_args.kwargs["blob_filter"] == ["a" * 64]
        assert mock_client.search.await_args.kwargs["top_k"] == 5

    @patch("oce.infrastructure.milvus3.search_store.Milvus3Client")
    async def test_empty_scope_searches_nothing(self, client_class, settings):
        store = Milvus3SearchStore(settings, dense_dim=DIM)

        assert await store.search(query_vector=[0.1] * DIM, allowed_blob_names=[]) == []
        client_class.return_value.search.assert_not_called()


def _hit(record: VectorRecord, score: float):
    from oce.domain.services.search import SearchHit

    return SearchHit(
        blob_name=record.blob_name,
        path=record.path,
        content=record.content,
        score=score,
        content_hash=record.content_hash,
        start_line=record.start_line,
        end_line=record.end_line,
    )
