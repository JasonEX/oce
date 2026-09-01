from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from oce import main


class _ContainerProvider:
    def __init__(self, container) -> None:
        self.container = container
        self.cache_clear = Mock()

    def __call__(self):
        return self.container


def _container(*, metrics_start_side_effect=None):
    worker = SimpleNamespace(start=AsyncMock())
    metrics = SimpleNamespace(start=AsyncMock(side_effect=metrics_start_side_effect))
    resource_sampler = SimpleNamespace(start=AsyncMock())
    monitoring_cleaner = SimpleNamespace(start=AsyncMock())
    return SimpleNamespace(
        worker=worker,
        metrics=metrics,
        resource_sampler=resource_sampler,
        monitoring_cleaner=monitoring_cleaner,
        ensure_index_compatible=AsyncMock(),
        close=AsyncMock(),
    )


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
        container.metrics.start.assert_awaited_once_with()
        container.resource_sampler.start.assert_awaited_once_with()
        container.monitoring_cleaner.start.assert_awaited_once_with()

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
