"""Periodic disk, memory and CPU samples into the metrics sink.

psutil is imported lazily; without it sampling is disabled with one log line
and startup is unaffected. Sampling is a side channel: a failure is logged.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable

from loguru import logger

from oce.infrastructure.metrics.periodic import PeriodicTask
from oce.shared.metrics import MetricsSink, ResourceSampleRecord

ResourceCollector = Callable[[], ResourceSampleRecord]


def _dir_size(path: str) -> int:
    """Total file bytes under ``path``; unreadable files are skipped, an unreadable root is 0."""
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    continue
    except OSError:
        return 0
    return total


def build_psutil_collector(data_dir: str | None) -> ResourceCollector | None:
    """A psutil-backed collector, or None when psutil is unavailable."""
    try:
        import psutil
    except ImportError:
        logger.warning("psutil not installed; resource sampling disabled")
        return None

    proc = psutil.Process()
    proc.cpu_percent(None)  # prime the baseline; the first reading is meaningless
    target = data_dir or os.getcwd()

    def _collect() -> ResourceSampleRecord:
        usage = shutil.disk_usage(target)
        return ResourceSampleRecord(
            disk_data_bytes=_dir_size(data_dir) if data_dir else 0,
            disk_free_bytes=usage.free,
            disk_total_bytes=usage.total,
            mem_rss_bytes=proc.memory_info().rss,
            mem_percent=float(proc.memory_percent()),
            cpu_percent=float(proc.cpu_percent(None)),
        )

    return _collect


class ResourceSampler(PeriodicTask):
    """Sample on an interval; a None collector disables the sampler."""

    def __init__(
        self,
        sink: MetricsSink,
        *,
        interval_seconds: float,
        collector: ResourceCollector | None,
    ) -> None:
        super().__init__(interval_seconds=interval_seconds, name="resource sample")
        self._sink = sink
        self._collector = collector

    async def start(self) -> None:
        if self._collector is None:
            return
        await super().start()

    async def _tick(self) -> None:
        if self._collector is None:
            return
        try:
            self._sink.record_resource_sample(self._collector())
        except Exception as exc:
            logger.warning("resource sample failed: {}", exc)
