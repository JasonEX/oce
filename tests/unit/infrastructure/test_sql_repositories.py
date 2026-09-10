"""Verify SQL repositories against an in-memory SQLite database."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from oce.domain.blob.blob import Blob, BlobStatus
from oce.domain.chunk import Chunk, ChunkRef
from oce.domain.services.search import SearchScope
from oce.infrastructure.persistence.lexical_index import create_lexical_table
from oce.infrastructure.persistence.models import ChunkModel
from oce.infrastructure.persistence.sql_blob_repo import SqlBlobRepository
from oce.infrastructure.persistence.sql_chain_repo import SqlChainRepository
from oce.infrastructure.persistence.sql_chunk_repo import SqlChunkRepository
from oce.infrastructure.persistence.sql_symbol_projection import SqlSymbolProjection
from oce.infrastructure.persistence.symbol_search_store import SymbolSearchStore
from oce.infrastructure.regex_symbol_provider import RegexSymbolProvider
from tests.conftest import make_sha256


def _blob_repository(session):
    return SqlBlobRepository(session)


@pytest.fixture
async def sqlite_session():
    """创建 SQLite 内存数据库 session"""
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )
    from sqlalchemy.orm import declarative_base

    from oce.infrastructure.persistence.models import (
        BlobChunkModel,
        BlobModel,
        ChainMemberModel,
        ChainModel,
        ChunkModel,
        SymbolOccurrenceModel,
    )

    Base = declarative_base()

    # 复制新表定义
    class TestBlob(Base):
        __table__ = BlobModel.__table__.to_metadata(Base.metadata)

    class TestChunk(Base):
        __table__ = ChunkModel.__table__.to_metadata(Base.metadata)

    class TestBlobChunk(Base):
        __table__ = BlobChunkModel.__table__.to_metadata(Base.metadata)

    class TestChain(Base):
        __table__ = ChainModel.__table__.to_metadata(Base.metadata)

    class TestChainMember(Base):
        __table__ = ChainMemberModel.__table__.to_metadata(Base.metadata)

    class TestSymbolOccurrence(Base):
        __table__ = SymbolOccurrenceModel.__table__.to_metadata(Base.metadata)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(create_lexical_table)

    async_session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    async with async_session_factory() as session:
        yield session
        await session.rollback()

    await engine.dispose()


@pytest.mark.asyncio
async def test_blob_repository_crud(sqlite_session):
    """测试 BlobRepository CRUD 操作"""
    repo = _blob_repository(sqlite_session)

    # 创建 Blob
    blob = Blob(
        blob_name=make_sha256("test1"),
        path="src/test.py",
        status=BlobStatus.PENDING,
        chunks=[
            ChunkRef(content_hash=make_sha256("chunk1"), start_line=1, end_line=10),
            ChunkRef(content_hash=make_sha256("chunk2"), start_line=11, end_line=20),
        ],
    )

    # 保存
    await repo.save(blob)
    await sqlite_session.commit()

    # 读取
    loaded = await repo.get(blob.blob_name)
    assert loaded is not None
    assert loaded.blob_name == blob.blob_name
    assert loaded.path == "src/test.py"
    assert loaded.status == BlobStatus.PENDING
    assert len(loaded.chunks) == 2

    # 判断存在
    assert await repo.exists_many([blob.blob_name]) == {blob.blob_name: True}

    # 更新状态
    loaded.mark_ready()
    await repo.save(loaded)
    await sqlite_session.commit()

    reloaded = await repo.get(blob.blob_name)
    assert reloaded.status == BlobStatus.READY

    # 删除
    await repo.delete(blob.blob_name)
    await sqlite_session.commit()

    deleted = await repo.get(blob.blob_name)
    assert deleted is None


@pytest.mark.asyncio
async def test_ready_sample_excludes_pending_blobs_and_respects_limit(sqlite_session):
    repo = _blob_repository(sqlite_session)
    await repo.save_many(
        [
            Blob(blob_name="a" * 64, path="a.py", status=BlobStatus.PENDING),
            Blob(blob_name="b" * 64, path="b.py", status=BlobStatus.READY),
            Blob(blob_name="c" * 64, path="c.py", status=BlobStatus.READY),
        ]
    )
    await sqlite_session.commit()
    assert await repo.list_ready_names(1) == ["b" * 64]
    assert await repo.list_ready_names(10) == ["b" * 64, "c" * 64]
    assert await repo.list_ready_names(0) == []


@pytest.mark.asyncio
async def test_expired_blob_remains_while_referenced_by_chain(sqlite_session):
    blob_repo = _blob_repository(sqlite_session)
    chain_repo = SqlChainRepository(sqlite_session)
    expired_at = datetime.now(timezone.utc) - timedelta(days=31)
    referenced = Blob(
        blob_name=make_sha256("referenced"),
        path="src/referenced.py",
        status=BlobStatus.READY,
        last_seen=expired_at,
    )
    orphan = Blob(
        blob_name=make_sha256("orphan"),
        path="src/orphan.py",
        status=BlobStatus.READY,
        last_seen=expired_at,
    )
    await blob_repo.save_many([referenced, orphan])
    chain = await chain_repo.create([referenced.blob_name])
    await sqlite_session.commit()

    assert await blob_repo.find_expired(30) == [orphan.blob_name]

    await chain_repo.delete(chain.chain_id)
    await sqlite_session.commit()
    assert set(await blob_repo.find_expired(30)) == {
        referenced.blob_name,
        orphan.blob_name,
    }


@pytest.mark.asyncio
async def test_chunk_repository_crud(sqlite_session):
    """测试 ChunkRepository CRUD 操作"""
    repo = SqlChunkRepository(sqlite_session)

    # 创建 Chunk
    chunk = Chunk(
        content_hash=make_sha256("content1"),
        path="src/test.py",
        content="print('hello')",
        start_line=1,
        end_line=1,
    )

    # 保存（内容寻址，重复写入幂等）
    await repo.save_many([chunk])
    await repo.save_many([chunk])
    await sqlite_session.commit()

    # 读取
    loaded = await sqlite_session.get(ChunkModel, chunk.content_hash)
    assert loaded is not None
    assert loaded.content == "print('hello')"
    assert loaded.embedded is False


async def _save_symbol_blobs(sqlite_session, specs):
    blob_repo = _blob_repository(sqlite_session)
    chunk_repo = SqlChunkRepository(sqlite_session)
    symbol_projection = SqlSymbolProjection(
        sqlite_session,
        RegexSymbolProvider(),
    )
    chunks = [
        Chunk(make_sha256(label), path, content, start_line, end_line)
        for label, path, content, start_line, end_line in specs
    ]
    names = [make_sha256(f"blob-{label}") for label, *_ in specs]
    await chunk_repo.save_many(chunks)
    blobs = [
        Blob(name, chunk.path, BlobStatus.READY, chunks=[chunk.to_ref()])
        for name, chunk in zip(names, chunks, strict=True)
    ]
    await blob_repo.save_many(blobs)
    for blob, chunk in zip(blobs, chunks, strict=True):
        # The provider reads whole files; pad so chunk lines stay absolute.
        file_content = "\n" * (chunk.start_line - 1) + chunk.content
        await symbol_projection.index(blob, [chunk], file_content)
    await sqlite_session.commit()
    return names, chunks


@pytest.mark.asyncio
async def test_blob_status_save_does_not_repeat_symbol_projection(sqlite_session):
    class CountingProvider:
        def __init__(self) -> None:
            self.calls = 0
            self._delegate = RegexSymbolProvider()

        def extract(self, **kwargs):
            self.calls += 1
            return self._delegate.extract(**kwargs)

    provider = CountingProvider()
    blob_repo = _blob_repository(sqlite_session)
    chunk_repo = SqlChunkRepository(sqlite_session)
    projection = SqlSymbolProjection(sqlite_session, provider)
    content = "def projected_once():\n    return True\n"
    chunk = Chunk(
        make_sha256("projected-once"),
        "src/once.py",
        content,
        1,
        2,
    )
    blob = Blob(
        make_sha256("blob-projected-once"),
        chunk.path,
        BlobStatus.PENDING,
        chunks=[chunk.to_ref()],
        language="python",
    )

    await chunk_repo.save_many([chunk])
    await blob_repo.save(blob)
    await projection.index(blob, [chunk], content)
    blob.mark_ready()
    await blob_repo.save(blob)
    await sqlite_session.commit()

    assert provider.calls == 1


@pytest.mark.asyncio
async def test_symbol_search_uses_chain_membership_and_request_deltas(sqlite_session):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    names, chunks = await _save_symbol_blobs(
        sqlite_session,
        [
            (
                "definition",
                "src/commands/copilot.rs",
                "#[tauri::command]\npub async fn copilot_get_models() {}",
                10,
                11,
            ),
            (
                "deleted",
                "src/deleted/copilot.rs",
                "#[tauri::command]\npub async fn copilot_get_models() {}",
                20,
                21,
            ),
            (
                "added",
                "src/config/copilot.rs",
                "pub async fn copilot_get_models() {}",
                30,
                30,
            ),
            (
                "outside",
                "vendor/copilot.rs",
                "pub fn copilot_get_models() {}",
                1,
                1,
            ),
        ],
    )
    chain = await SqlChainRepository(sqlite_session).create([names[0], names[1]])
    await sqlite_session.commit()
    factory = async_sessionmaker(
        sqlite_session.bind, class_=AsyncSession, expire_on_commit=False
    )
    store = SymbolSearchStore(factory)

    hits = await store.search_exact(
        identifiers=["copilot_get_models"],
        scope=SearchScope(
            blob_names=frozenset({names[0], names[2]}),
            chain_id=chain.chain_id,
            chain_version=chain.version,
            added_blob_names=frozenset({names[2]}),
            deleted_blob_names=frozenset({names[1]}),
        ),
        top_k=10,
    )

    assert [hit.path for hit in hits] == [chunks[0].path, chunks[2].path]


@pytest.mark.asyncio
async def test_symbol_search_keeps_exact_recall_for_large_checkpoint(sqlite_session):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    names, chunks = await _save_symbol_blobs(
        sqlite_session,
        [
            (
                "target",
                "src/target.py",
                "def target_symbol(): pass",
                1,
                1,
            )
        ],
    )
    members = [
        names[0],
        *(make_sha256(f"member-{index}") for index in range(2_000)),
    ]
    chain = await SqlChainRepository(sqlite_session).create(members)
    await sqlite_session.commit()
    factory = async_sessionmaker(
        sqlite_session.bind, class_=AsyncSession, expire_on_commit=False
    )
    store = SymbolSearchStore(factory)

    hits = await store.search_exact(
        identifiers=["target_symbol"],
        scope=SearchScope(
            blob_names=frozenset(members),
            chain_id=chain.chain_id,
            chain_version=chain.version,
        ),
        top_k=10,
    )

    assert [hit.path for hit in hits] == [chunks[0].path]

    stale_relation_hits = await store.search_exact(
        identifiers=["target_symbol"],
        scope=SearchScope(
            blob_names=frozenset(members),
            chain_id=chain.chain_id,
            chain_version=chain.version + 1,
        ),
        top_k=10,
    )
    assert [hit.path for hit in stale_relation_hits] == [chunks[0].path]


@pytest.mark.asyncio
async def test_symbol_search_batches_large_added_only_scope(sqlite_session):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    names, chunks = await _save_symbol_blobs(
        sqlite_session,
        [("target", "src/target.py", "def target_symbol(): pass", 1, 1)],
    )
    scope_names = frozenset(
        [names[0], *(make_sha256(f"added-{index}") for index in range(1_200))]
    )
    factory = async_sessionmaker(
        sqlite_session.bind, class_=AsyncSession, expire_on_commit=False
    )

    hits = await SymbolSearchStore(factory).search_exact(
        identifiers=["target_symbol"],
        scope=SearchScope(blob_names=scope_names, added_blob_names=scope_names),
        top_k=10,
    )

    assert [hit.path for hit in hits] == [chunks[0].path]


@pytest.mark.asyncio
async def test_chain_repository_checkpoint(sqlite_session):
    """测试 ChainRepository checkpoint 操作"""
    repo = SqlChainRepository(sqlite_session)

    # 创建 Chain
    members = [make_sha256("blob1"), make_sha256("blob2")]
    chain = await repo.create(members)
    await sqlite_session.commit()

    assert chain.chain_id is not None
    assert chain.version == 1
    assert len(chain.members) == 2

    # 获取成员
    loaded_members = await repo.get_members(chain.chain_id)
    assert len(loaded_members) == 2
    assert set(members) == loaded_members

    # 应用 checkpoint
    new_version = await repo.apply_checkpoint(
        chain.chain_id,
        chain.version,
        added=[make_sha256("blob3")],
        deleted=[make_sha256("blob1")],
    )
    await sqlite_session.commit()

    assert new_version == 2

    # 验证成员变更
    updated_members = await repo.get_members(chain.chain_id)
    assert len(updated_members) == 2
    assert make_sha256("blob2") in updated_members
    assert make_sha256("blob3") in updated_members
    assert make_sha256("blob1") not in updated_members


@pytest.mark.asyncio
async def test_chain_repository_rejects_stale_checkpoint(sqlite_session):
    repo = SqlChainRepository(sqlite_session)
    original = make_sha256("blob1")
    chain = await repo.create([original])
    await sqlite_session.commit()

    new_version = await repo.apply_checkpoint(
        chain.chain_id,
        chain.version,
        added=[make_sha256("blob2")],
        deleted=[],
    )
    assert new_version == 2

    stale_result = await repo.apply_checkpoint(
        chain.chain_id,
        chain.version,
        added=[make_sha256("blob3")],
        deleted=[original],
    )

    assert stale_result is None
    loaded = await repo.get(chain.chain_id)
    assert loaded is not None
    assert loaded.version == 2
    assert loaded.members == {original, make_sha256("blob2")}


@pytest.mark.asyncio
async def test_chain_repository_batches_large_member_sets(sqlite_session):
    repo = SqlChainRepository(sqlite_session)
    members = [make_sha256(f"blob-{index}") for index in range(17_000)]

    chain = await repo.create(members)
    await sqlite_session.commit()

    assert len(await repo.get_members(chain.chain_id)) == len(members)


@pytest.mark.asyncio
async def test_batch_operations(sqlite_session):
    """测试批量操作"""
    blob_repo = _blob_repository(sqlite_session)
    chunk_repo = SqlChunkRepository(sqlite_session)

    # 批量保存 Chunk
    chunks = [
        Chunk(
            content_hash=make_sha256(f"chunk{i}"),
            path="src/test.py",
            content=f"line {i}",
            start_line=i,
            end_line=i,
        )
        for i in range(1, 6)
    ]
    await chunk_repo.save_many(chunks)

    # 批量保存 Blob
    blobs = [
        Blob(
            blob_name=make_sha256(f"blob{i}"),
            path=f"src/file{i}.py",
            status=BlobStatus.PENDING,
            chunks=[
                ChunkRef(
                    content_hash=make_sha256(f"chunk{i}"), start_line=i, end_line=i
                )
            ],
        )
        for i in range(1, 4)
    ]
    await blob_repo.save_many(blobs)
    await sqlite_session.commit()

    # 批量读取
    blob_names = [make_sha256(f"blob{i}") for i in range(1, 4)]
    loaded_blobs = await blob_repo.get_many(blob_names)
    assert len(loaded_blobs) == 3

    # 批量判断存在
    exists_map = await blob_repo.exists_many(blob_names)
    assert all(exists_map.values())


@pytest.mark.asyncio
async def test_blob_delete_only_removes_unreferenced_chunks(sqlite_session):
    blob_repo = _blob_repository(sqlite_session)
    chunk_repo = SqlChunkRepository(sqlite_session)
    shared = Chunk(make_sha256("shared"), "", "shared", 1, 1)
    unique = Chunk(make_sha256("unique"), "", "unique", 2, 2)
    await chunk_repo.save_many([shared, unique])
    first_name = make_sha256("first-blob")
    second_name = make_sha256("second-blob")
    await blob_repo.save_many(
        [
            Blob(
                first_name,
                "first.py",
                chunks=[shared.to_ref(), unique.to_ref()],
            ),
            Blob(second_name, "second.py", chunks=[shared.to_ref()]),
        ]
    )
    await sqlite_session.commit()

    await blob_repo.delete(first_name)
    await sqlite_session.commit()

    remaining = await sqlite_session.scalars(select(ChunkModel.content_hash))
    assert set(remaining) == {shared.content_hash}
