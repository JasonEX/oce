"""Milvus 3.0 集成测试

使用 Milvus Lite（本地文件数据库）进行真实测试。
无需 Docker，数据存储在 ./test_data/milvus.db
"""

import math
import random
import shutil

import pytest

from oce.domain.services.search import VectorRecord
from oce.infrastructure.milvus3.client import Milvus3Client
from oce.infrastructure.milvus3.path_index import PathIndexClient
from oce.infrastructure.milvus3.search_store import Milvus3SearchStore
from oce.shared.config.settings import MilvusSettings


@pytest.mark.integration
async def test_local_index_upgrade_preserves_vectors_and_scoped_nearest_neighbors(
    tmp_path,
):
    settings = MilvusSettings(
        endpoint=str(tmp_path / "scoped.db"), dense_index_type="HNSW"
    )
    rng = random.Random(7281)
    query = [rng.random() for _ in range(8)]
    records = [
        VectorRecord(
            chunk_id=f"{index:064x}",
            content_hash=f"content-{index}",
            blob_name=("a" if index < 32 else "b") * 64,
            path=f"src/{index}.py",
            content="source",
            start_line=1,
            end_line=1,
            vector=[rng.random() for _ in range(8)],
        )
        for index in range(192)
    ]
    original = Milvus3Client(settings, dense_dim=8)
    try:
        await original.insert(records)
    finally:
        await original.close()

    upgraded = Milvus3Client(
        settings.model_copy(update={"dense_index_type": "FLAT"}), dense_dim=8
    )
    try:
        hits = await upgraded.search(query, blob_filter=["a" * 64], top_k=20)

        def cosine(record):
            return sum(a * b for a, b in zip(query, record.vector, strict=True)) / (
                math.sqrt(sum(v * v for v in query))
                * math.sqrt(sum(v * v for v in record.vector))
            )

        expected = sorted(records[:32], key=cosine, reverse=True)[:20]
        assert [hit.content_hash for hit in hits] == [
            record.content_hash for record in expected
        ]
        assert all(hit.blob_name == "a" * 64 for hit in hits)
        assert (await upgraded.index_stats()).entities == len(records)
    finally:
        await upgraded.close()


@pytest.fixture(scope="module")
def test_db_path(tmp_path_factory):
    """创建临时测试数据库目录"""
    db_dir = tmp_path_factory.mktemp("milvus_test")
    db_file = db_dir / "milvus.db"
    yield str(db_file)

    # 测试结束后清理（忽略 Windows 文件锁错误）
    try:
        import time

        time.sleep(0.5)  # 等待 Milvus Lite 释放文件
        if db_dir.exists():
            shutil.rmtree(db_dir, ignore_errors=True)
    except Exception:
        pass  # 忽略清理错误


@pytest.fixture(scope="module")
def milvus_settings(test_db_path):
    """Milvus Lite 配置（本地文件数据库）"""
    return MilvusSettings(
        endpoint=test_db_path,  # Milvus Lite：直接传文件路径
        token=None,
        collection_name="test_oce_chunks",
    )


@pytest.fixture
async def milvus_client(milvus_settings):
    """初始化 Milvus 客户端（每个测试独立）"""
    client = Milvus3Client(milvus_settings, dense_dim=128)  # 小维度加快测试

    await client.initialize()

    # 清空已有数据（确保测试隔离）
    try:
        await client._client.delete(
            collection_name=milvus_settings.collection_name,
            filter="content_hash != ''",  # 删除所有数据
        )
    except Exception:
        pass  # Collection 可能是空的

    yield client
    await client.close()


@pytest.mark.integration
@pytest.mark.asyncio
class TestMilvus3Integration:
    """Milvus 3.0 集成测试（使用 Milvus Lite）"""

    async def test_insert_and_search(self, milvus_client):
        """测试完整的插入和检索流程"""
        # 插入测试数据
        chunks = [
            VectorRecord(
                chunk_id="1" * 64,
                content_hash="hash1",
                blob_name="a" * 64,
                path="src/math.py",
                content="def calculate_sum(a, b): return a + b",
                start_line=1,
                end_line=1,
                vector=[0.1] * 128,
            ),
            VectorRecord(
                chunk_id="2" * 64,
                content_hash="hash2",
                blob_name="a" * 64,
                path="src/math.py",
                content="def calculate_product(a, b): return a * b",
                start_line=3,
                end_line=3,
                vector=[0.2] * 128,
            ),
            VectorRecord(
                chunk_id="3" * 64,
                content_hash="hash3",
                blob_name="b" * 64,
                path="src/calculator.py",
                content="class Calculator: pass",
                start_line=1,
                end_line=1,
                vector=[0.3] * 128,
            ),
        ]

        assert await milvus_client.insert(chunks) == 3

        # dense 向量检索
        results = await milvus_client.search(
            [0.15] * 128,  # 接近 hash1
            blob_filter=["a" * 64],
            top_k=2,
        )

        assert len(results) >= 1
        assert results[0].content_hash in ["hash1", "hash2"]
        assert results[0].blob_name == "a" * 64

    async def test_blob_filter(self, milvus_client):
        """测试 blob_name 过滤"""
        # 插入测试数据（两个不同的 blob）
        chunks = [
            VectorRecord(
                chunk_id="4" * 64,
                content_hash="filter_hash1",
                blob_name="b" * 64,
                path="src/calculator.py",
                content="class Calculator: pass",
                start_line=1,
                end_line=1,
                vector=[0.3] * 128,
            ),
            VectorRecord(
                chunk_id="5" * 64,
                content_hash="filter_hash2",
                blob_name="a" * 64,
                path="src/math.py",
                content="def add(a, b): return a + b",
                start_line=1,
                end_line=1,
                vector=[0.2] * 128,
            ),
        ]
        await milvus_client.insert(chunks)

        # 只搜索 calculator.py
        results = await milvus_client.search(
            [0.3] * 128,
            blob_filter=["b" * 64],
            top_k=10,
        )

        assert len(results) >= 1
        for result in results:
            assert result.blob_name == "b" * 64

    async def test_delete_by_blob(self, milvus_client):
        """测试按 blob 删除"""
        # 插入测试数据
        chunks = [
            VectorRecord(
                chunk_id="6" * 64,
                content_hash="delete_hash1",
                blob_name="c" * 64,
                path="src/delete_me.py",
                content="class ToDelete: pass",
                start_line=1,
                end_line=1,
                vector=[0.4] * 128,
            ),
        ]
        await milvus_client.insert(chunks)

        # 删除
        await milvus_client.delete_by_blob_names(["c" * 64])

        # 验证删除后搜索不到
        results = await milvus_client.search(
            [0.4] * 128,
            blob_filter=["c" * 64],
            top_k=10,
        )

        assert len(results) == 0


@pytest.mark.integration
@pytest.mark.asyncio
class TestMilvus3SearchStoreIntegration:
    """Milvus3SearchStore 集成测试"""

    @pytest.fixture
    async def search_store(self, milvus_settings):
        """初始化 SearchStore"""
        store = Milvus3SearchStore(milvus_settings, dense_dim=128)
        await store.client.initialize()
        yield store
        await store.close()

    async def test_search_store_upsert_and_search(self, search_store):
        """测试 SearchStore 的 upsert 和 search"""
        # Upsert 数据
        items = [
            VectorRecord(
                chunk_id="test_hash_1",
                content_hash="test_hash_1",
                blob_name="d" * 64,
                path="test.py",
                content="def test_function(): pass",
                start_line=1,
                end_line=1,
                vector=[0.5] * 128,
            )
        ]

        await search_store.upsert(items)

        # 搜索
        results = await search_store.search(
            query_vector=[0.5] * 128,
            allowed_blob_names=["d" * 64],
            top_k=5,
            vector_threshold=0.0,
        )

        assert len(results) >= 1
        assert results[0].blob_name == "d" * 64
        assert results[0].path == "test.py"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_path_index_lite_lifecycle(tmp_path):
    settings = MilvusSettings(
        endpoint=str(tmp_path / "paths.db"),
        path_collection_name="test_oce_paths",
    )
    client = PathIndexClient(settings, dense_dim=8)
    first_blob = "a" * 64
    second_blob = "b" * 64
    try:
        inserted = await client.insert(
            [
                {
                    "path_id": f"path_{first_blob}",
                    "blob_name": first_blob,
                    "path": "src/auth.py",
                    "path_document": "auth python source file",
                    "path_vector": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                },
                {
                    "path_id": f"path_{second_blob}",
                    "blob_name": second_blob,
                    "path": "src/cache.py",
                    "path_document": "cache python source file",
                    "path_vector": [0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                },
            ]
        )

        hits = await client.search_paths(
            [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            allowed_blob_names=[first_blob],
            top_k=2,
        )
        stats = await client.index_stats()

        assert inserted == {"inserted": 2}
        assert [(hit.blob_name, hit.path) for hit in hits] == [
            (first_blob, "src/auth.py")
        ]
        assert stats.exists is True
        assert stats.entities == 2

        await client.delete_by_blob_names([first_blob])
        assert (
            await client.search_paths(
                [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                allowed_blob_names=[first_blob],
                top_k=2,
            )
            == []
        )
    finally:
        await client.close()
