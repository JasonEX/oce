"""Startup probes use repository contracts and cannot hang startup."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from oce.application.warmup import warm_retrieval_stores


def _factory(names=(), error=None):
    sample = AsyncMock(return_value=list(names), side_effect=error)

    @asynccontextmanager
    async def factory():
        yield SimpleNamespace(blobs=SimpleNamespace(list_ready_names=sample))

    return factory, sample


async def test_warm_up_uses_bounded_ready_sample_and_continues_after_probe_failure():
    factory, sample = _factory(["a" * 64, "b" * 64])
    dense = SimpleNamespace(search=AsyncMock(return_value=[]))
    paths = SimpleNamespace(search_paths=AsyncMock(side_effect=RuntimeError("cold")))
    lexical = SimpleNamespace(search_lexical=AsyncMock(return_value=[]))
    timings = await warm_retrieval_stores(
        uow_factory=factory,
        search_store=dense,
        dimensions=4,
        path_store=paths,
        lexical_store=lexical,
        sample=2,
    )
    sample.assert_awaited_once_with(2)
    assert set(timings) == {"dense", "lexical"}
    dense.search.assert_awaited_once_with(
        query_vector=[0.5] * 4, allowed_blob_names=["a" * 64, "b" * 64], top_k=1
    )
    assert lexical.search_lexical.call_args.kwargs["scope"].blob_names == {
        "a" * 64,
        "b" * 64,
    }


@pytest.mark.parametrize("error", [None, RuntimeError("database unavailable")])
async def test_empty_or_failed_sampling_does_not_probe(error):
    factory, _ = _factory(error=error)
    dense = SimpleNamespace(search=AsyncMock())
    assert (
        await warm_retrieval_stores(
            uow_factory=factory, search_store=dense, dimensions=4
        )
        == {}
    )
    dense.search.assert_not_awaited()


async def test_a_hanging_probe_times_out_and_the_next_store_still_warms():
    factory, _ = _factory(["a" * 64])
    cancelled = asyncio.Event()

    async def hanging(**kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    lexical = SimpleNamespace(search_lexical=AsyncMock(return_value=[]))
    timings = await warm_retrieval_stores(
        uow_factory=factory,
        search_store=SimpleNamespace(search=hanging),
        dimensions=4,
        lexical_store=lexical,
        timeout_seconds=0.01,
    )
    assert cancelled.is_set()
    assert set(timings) == {"lexical"}


async def test_shutdown_cancellation_is_not_swallowed():
    factory, _ = _factory(["a" * 64])
    lexical = SimpleNamespace(search_lexical=AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        await warm_retrieval_stores(
            uow_factory=factory,
            search_store=SimpleNamespace(
                search=AsyncMock(side_effect=asyncio.CancelledError)
            ),
            dimensions=4,
            lexical_store=lexical,
        )
    lexical.search_lexical.assert_not_awaited()
