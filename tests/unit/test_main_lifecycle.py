import asyncio
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from oce import main
from oce.application.container import Container, ModelRuntime


class _ContainerProvider:
    def __init__(self, container) -> None:
        self.container = container
        self.cache_clear = Mock()

    def __call__(self):
        return self.container


def _container(*, metrics_start_side_effect=None):
    """A stand-in graph whose ``start`` is the real ``Container.start``."""
    worker = SimpleNamespace(start=AsyncMock())
    monitoring = SimpleNamespace(
        metrics=SimpleNamespace(start=AsyncMock(side_effect=metrics_start_side_effect)),
        resource_sampler=SimpleNamespace(start=AsyncMock()),
        cleaner=SimpleNamespace(start=AsyncMock()),
    )
    container = SimpleNamespace(
        worker=worker,
        monitoring=monitoring,
        ensure_index_compatible=AsyncMock(return_value=True),
        start_worker=worker.start,
        warm_up=AsyncMock(),
        close=AsyncMock(),
    )
    container.start = partial(Container.start, container)
    return container


def _patch_lifespan_dependencies(monkeypatch, container):
    provider = _ContainerProvider(container)
    monkeypatch.setattr(main, "get_container", provider)
    monkeypatch.setattr(main, "get_settings", lambda: SimpleNamespace(log=Mock()))
    monkeypatch.setattr(main, "configure_logging", Mock())
    dispose = AsyncMock()
    monkeypatch.setattr(main, "engine", SimpleNamespace(dispose=dispose))
    return provider, dispose


async def test_lifespan_closes_resources_and_clears_cached_container(monkeypatch):
    container = _container()
    provider, dispose = _patch_lifespan_dependencies(monkeypatch, container)

    async with main.lifespan(main.app):
        container.ensure_index_compatible.assert_awaited_once_with()
        container.worker.start.assert_awaited_once_with()
        container.monitoring.metrics.start.assert_awaited_once_with()
        container.monitoring.resource_sampler.start.assert_awaited_once_with()
        container.monitoring.cleaner.start.assert_awaited_once_with()
        container.warm_up.assert_awaited_once_with()

    container.close.assert_awaited_once_with()
    provider.cache_clear.assert_called_once_with()
    dispose.assert_awaited_once_with()


async def test_lifespan_cleans_up_after_partial_startup(monkeypatch):
    container = _container(metrics_start_side_effect=RuntimeError("metrics failed"))
    provider, dispose = _patch_lifespan_dependencies(monkeypatch, container)

    with pytest.raises(RuntimeError, match="metrics failed"):
        async with main.lifespan(main.app):
            pass

    container.close.assert_awaited_once_with()
    provider.cache_clear.assert_called_once_with()
    dispose.assert_awaited_once_with()


async def test_requests_start_only_after_storage_probes_finish(monkeypatch):
    container = _container()
    entered, finish, serving = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def warm_up():
        entered.set()
        await finish.wait()

    container.warm_up.side_effect = warm_up
    _patch_lifespan_dependencies(monkeypatch, container)

    async def start():
        async with main.lifespan(main.app):
            serving.set()

    task = asyncio.create_task(start())
    await entered.wait()
    assert not serving.is_set()
    finish.set()
    await task
    assert serving.is_set()


async def test_deferred_index_readiness_does_not_consume_pending_work(monkeypatch):
    container = _container()
    container.ensure_index_compatible.return_value = False
    _patch_lifespan_dependencies(monkeypatch, container)

    async with main.lifespan(main.app):
        container.worker.start.assert_not_awaited()
        container.monitoring.metrics.start.assert_awaited_once_with()

    container.close.assert_awaited_once_with()


@pytest.mark.parametrize("failure", [None, "worker", "metrics", "dense", "models"])
async def test_container_close_finishes_all_cleanup_in_order(
    failure: str | None,
) -> None:
    calls: list[str] = []

    def cleanup(name: str) -> AsyncMock:
        async def run() -> None:
            calls.append(name)
            if name == failure:
                raise RuntimeError(name)

        return AsyncMock(side_effect=run)

    container = SimpleNamespace(
        worker=SimpleNamespace(stop=cleanup("worker")),
        monitoring=SimpleNamespace(
            resource_sampler=SimpleNamespace(stop=cleanup("sampler")),
            cleaner=SimpleNamespace(stop=cleanup("cleaner")),
            metrics=SimpleNamespace(stop=cleanup("metrics")),
        ),
        stores=SimpleNamespace(
            search_store=SimpleNamespace(close=cleanup("dense")),
            path_index=SimpleNamespace(close=cleanup("path")),
        ),
        models=SimpleNamespace(close=cleanup("models")),
        queue=SimpleNamespace(close=cleanup("queue")),
    )

    if failure is None:
        await Container.close(container)
    else:
        with pytest.raises(RuntimeError, match=failure):
            await Container.close(container)

    assert calls == [
        "worker",
        "sampler",
        "cleaner",
        "metrics",
        "dense",
        "path",
        "models",
        "queue",
    ]


async def test_model_close_finishes_clients_and_reports_all_failures() -> None:
    embed_error, rerank_error = RuntimeError("embedding"), RuntimeError("rerank")
    runtime = SimpleNamespace(
        embedder=SimpleNamespace(close=AsyncMock(side_effect=embed_error)),
        reranker=SimpleNamespace(close=AsyncMock(side_effect=rerank_error)),
        llm_clients=(
            SimpleNamespace(close=AsyncMock()),
            SimpleNamespace(close=AsyncMock()),
        ),
    )

    with pytest.raises(ExceptionGroup, match="Resource cleanup failed") as raised:
        await ModelRuntime.close(runtime)

    assert raised.value.exceptions == (embed_error, rerank_error)
    runtime.embedder.close.assert_awaited_once_with()
    runtime.reranker.close.assert_awaited_once_with()
    for client in runtime.llm_clients:
        client.close.assert_awaited_once_with()


async def test_lifespan_disposes_engine_when_close_fails(monkeypatch) -> None:
    container = _container()
    container.close.side_effect = RuntimeError("cleanup failed")
    provider, dispose = _patch_lifespan_dependencies(monkeypatch, container)

    with pytest.raises(RuntimeError, match="cleanup failed"):
        async with main.lifespan(main.app):
            pass

    provider.cache_clear.assert_called_once_with()
    dispose.assert_awaited_once_with()
