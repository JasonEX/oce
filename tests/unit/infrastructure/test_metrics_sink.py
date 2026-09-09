"""SqlMetricsSink 异步落库测试。

用 StaticPool 的内存库让多个 session 共享同一连接（默认 :memory: 每连接独立库）。
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import oce.infrastructure.persistence.models  # noqa: F401  注册 ORM 表到 Base.metadata
from oce.infrastructure.metrics.sql_metrics_sink import SqlMetricsSink
from oce.infrastructure.persistence.models import (
    ApiCallMetricModel,
    ResourceSampleModel,
    RetrievalMetricModel,
    TokenUsageMetricModel,
)
from oce.shared.database.session import Base
from oce.shared.metrics import (
    ApiCallRecord,
    NoopMetricsSink,
    ResourceSampleRecord,
    RetrievalMetricRecord,
    TokenUsageRecord,
)


async def _make_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return factory, engine


async def _count(factory, model) -> int:
    async with factory() as session:
        return await session.scalar(select(func.count()).select_from(model))


async def test_flush_writes_all_three_kinds():
    factory, engine = await _make_factory()
    try:
        sink = SqlMetricsSink(factory, flush_interval_seconds=999, max_buffer=100)
        sink.record_api_call(
            ApiCallRecord(
                endpoint="/agents/codebase-retrieval",
                method="POST",
                status_code=200,
                latency_ms=12,
            )
        )
        sink.record_token_usage(
            TokenUsageRecord(kind="embed", model="m", total_tokens=10, credential_id=1)
        )
        sink.record_resource_sample(
            ResourceSampleRecord(
                disk_data_bytes=1,
                disk_free_bytes=2,
                disk_total_bytes=3,
                mem_rss_bytes=4,
                mem_percent=5.0,
                cpu_percent=6.0,
            )
        )
        await sink._flush_once()

        assert await _count(factory, ApiCallMetricModel) == 1
        assert await _count(factory, TokenUsageMetricModel) == 1
        assert await _count(factory, ResourceSampleModel) == 1
    finally:
        await engine.dispose()


async def test_flush_drains_buffer_no_double_write():
    factory, engine = await _make_factory()
    try:
        sink = SqlMetricsSink(factory, flush_interval_seconds=999, max_buffer=100)
        sink.record_token_usage(
            TokenUsageRecord(kind="rerank", model="m", total_tokens=5)
        )
        await sink._flush_once()
        await sink._flush_once()  # 第二次缓冲已空，不应重复写
        assert await _count(factory, TokenUsageMetricModel) == 1
    finally:
        await engine.dispose()


async def test_failed_flush_retains_records_for_later_flush():
    factory, engine = await _make_factory()

    class FailingSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

        def add_all(self, rows):
            return None

        async def commit(self):
            raise RuntimeError("database busy")

    try:
        sink = SqlMetricsSink(lambda: FailingSession(), flush_interval_seconds=999)
        sink.record_api_call(
            ApiCallRecord(
                endpoint="/health", method="GET", status_code=200, latency_ms=1
            )
        )

        await sink._flush_once()
        assert await _count(factory, ApiCallMetricModel) == 0

        sink._session_factory = factory
        await sink._flush_once()
        assert await _count(factory, ApiCallMetricModel) == 1
    finally:
        await engine.dispose()


async def test_failed_flush_keeps_newest_records_with_bounded_buffer():
    factory, engine = await _make_factory()
    sink = SqlMetricsSink(factory, flush_interval_seconds=999, max_buffer=2)
    older = [
        ApiCallRecord(
            endpoint=f"/old-{index}", method="GET", status_code=200, latency_ms=1
        )
        for index in range(2)
    ]
    batch = sink._drain()
    assert len(batch) == 0

    for record in older:
        sink.record_api_call(record)
    batch = sink._drain()
    sink.record_api_call(
        ApiCallRecord(endpoint="/new", method="GET", status_code=200, latency_ms=1)
    )
    sink._restore(batch)

    try:
        await sink._flush_once()
        async with factory() as session:
            endpoints = list(
                (
                    await session.execute(
                        select(ApiCallMetricModel.endpoint).order_by(
                            ApiCallMetricModel.id
                        )
                    )
                ).scalars()
            )
        assert endpoints == ["/old-1", "/new"]
    finally:
        await engine.dispose()


async def test_start_stop_flushes_remaining():
    factory, engine = await _make_factory()
    try:
        sink = SqlMetricsSink(factory, flush_interval_seconds=999, max_buffer=100)
        await sink.start()
        sink.record_api_call(
            ApiCallRecord(
                endpoint="/health", method="GET", status_code=200, latency_ms=1
            )
        )
        await sink.stop()  # stop 前应 flush 掉剩余
        assert await _count(factory, ApiCallMetricModel) == 1
    finally:
        await engine.dispose()


async def test_buffer_maxlen_drops_oldest():
    factory, engine = await _make_factory()
    try:
        sink = SqlMetricsSink(factory, flush_interval_seconds=999, max_buffer=2)
        for i in range(5):
            sink.record_api_call(
                ApiCallRecord(
                    endpoint=f"/e{i}", method="GET", status_code=200, latency_ms=i
                )
            )
        await sink._flush_once()
        assert await _count(factory, ApiCallMetricModel) == 2  # maxlen=2，仅留最新两条
    finally:
        await engine.dispose()


async def test_flush_writes_retrieval_with_stage_columns():
    factory, engine = await _make_factory()
    try:
        sink = SqlMetricsSink(factory, flush_interval_seconds=999, max_buffer=100)
        sink.record_retrieval(
            RetrievalMetricRecord(
                source="retrieval",
                hit_count=0,
                total_ms=42,
                scope_size=3,
                intent="symbol",
                path_boosted=True,
                rerank_route="dedicated+llm",
                dense_route="skip:exact_definition",
                head_slots=2,
                query_text="q",
                stages={
                    "embed": 7,
                    "dense": 10,
                    "path": 3,
                    "path_lookup": 2,
                    "lexical": 4,
                    "select": 5,
                    "expand": 6,
                },
            )
        )
        await sink._flush_once()

        async with factory() as session:
            row = (await session.execute(select(RetrievalMetricModel))).scalar_one()
        assert row.source == "retrieval"
        assert row.hit_count == 0  # 空回也落库
        assert row.total_ms == 42
        assert row.dense_route == "skip:exact_definition"
        assert row.embed_ms == 7
        assert row.dense_ms == 10
        assert row.path_ms == 3
        assert row.path_lookup_ms == 2
        assert row.lexical_ms == 4
        assert row.select_ms == 5
        assert row.expand_ms == 6
        assert row.exact_ms is None  # 未跑的阶段留空，不冒充 0
        assert row.path_boosted is True
        assert row.rerank_route == "dedicated+llm"
        assert row.head_slots == 2
        assert row.query_text == "q"
    finally:
        await engine.dispose()


async def test_noop_sink_is_inert():
    sink = NoopMetricsSink()
    sink.record_api_call(
        ApiCallRecord(endpoint="/x", method="GET", status_code=200, latency_ms=1)
    )
    await sink.start()
    await sink.stop()
