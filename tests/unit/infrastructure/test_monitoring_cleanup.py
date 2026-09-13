"""MonitoringCleaner: expired rows go, recent rows stay, failures are absorbed.

A StaticPool in-memory database lets several sessions share one connection;
by default every :memory: connection is its own database.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import oce.infrastructure.persistence.models  # noqa: F401  registers the ORM tables on Base.metadata
from oce.infrastructure.metrics.cleanup import MonitoringCleaner
from oce.infrastructure.persistence.models import (
    ApiCallMetricModel,
    ResourceSampleModel,
    RetrievalMetricModel,
    TokenUsageMetricModel,
)
from oce.shared.database.session import Base


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


def _old() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=40)


def _recent() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=1)


async def _count(factory, model) -> int:
    async with factory() as session:
        return await session.scalar(select(func.count()).select_from(model))


async def test_cleanup_deletes_expired_keeps_recent():
    factory, engine = await _make_factory()
    try:
        async with factory() as session:
            session.add_all(
                [
                    ApiCallMetricModel(
                        ts=_old(),
                        endpoint="/x",
                        method="GET",
                        status_code=200,
                        latency_ms=1,
                    ),
                    ApiCallMetricModel(
                        ts=_recent(),
                        endpoint="/y",
                        method="GET",
                        status_code=200,
                        latency_ms=1,
                    ),
                    TokenUsageMetricModel(
                        ts=_old(), kind="embed", model="m", total_tokens=1
                    ),
                    ResourceSampleModel(
                        ts=_old(),
                        disk_data_bytes=1,
                        disk_free_bytes=2,
                        disk_total_bytes=3,
                        mem_rss_bytes=4,
                        mem_percent=5.0,
                        cpu_percent=6.0,
                    ),
                    RetrievalMetricModel(
                        ts=_recent(), source="retrieval", hit_count=1, total_ms=5
                    ),
                ]
            )
            await session.commit()

        cleaner = MonitoringCleaner(factory, retention_days=30, interval_seconds=999)
        removed = await cleaner._cleanup_once()

        assert removed == 3  # three rows from 40 days ago (api, token, resource)
        assert await _count(factory, ApiCallMetricModel) == 1  # recent rows stay
        assert await _count(factory, TokenUsageMetricModel) == 0
        assert await _count(factory, ResourceSampleModel) == 0
        assert await _count(factory, RetrievalMetricModel) == 1  # recent rows stay
    finally:
        await engine.dispose()


async def test_cleanup_swallows_errors():
    """A raising session factory makes cleanup return 0 rather than raise."""

    def _boom():
        raise RuntimeError("db down")

    cleaner = MonitoringCleaner(_boom, retention_days=30, interval_seconds=999)
    assert await cleaner._cleanup_once() == 0


async def test_loop_invokes_cleanup_periodically():
    """The loop drives _cleanup_once on its interval; a stub counter avoids in-memory connection races on cancel."""
    calls = {"n": 0}

    async def _fake_cleanup() -> int:
        calls["n"] += 1
        return 0

    cleaner = MonitoringCleaner(lambda: None, retention_days=30, interval_seconds=0.01)
    cleaner._cleanup_once = _fake_cleanup
    await cleaner.start()
    await asyncio.sleep(0.05)
    await cleaner.stop()

    assert calls["n"] >= 1
