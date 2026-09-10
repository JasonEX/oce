"""Bounded storage probes before the application begins accepting requests.

Cold stores can time out during recall and silently omit a retrieval lane.
Probes pay that initialization cost before serving; a failed probe is logged
and leaves that store cold rather than preventing the application starting.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from time import perf_counter

from loguru import logger

from oce.application.uow import UnitOfWorkFactory
from oce.domain.services.path_search import PathSearchStore
from oce.domain.services.search import LexicalSearchStore, SearchScope, SearchStore

WARM_UP_SAMPLE = 256


async def warm_retrieval_stores(
    *,
    uow_factory: UnitOfWorkFactory,
    search_store: SearchStore,
    dimensions: int,
    path_store: PathSearchStore | None = None,
    lexical_store: LexicalSearchStore | None = None,
    sample: int = WARM_UP_SAMPLE,
    timeout_seconds: float = 10.0,
) -> dict[str, int]:
    """Run one small search per store; returns the milliseconds each took.

    Both sampling and each probe are bounded. Cancellation still propagates
    so shutdown never starts another probe.
    """
    if dimensions < 1 or sample < 1 or timeout_seconds <= 0:
        raise ValueError("Warm-up dimensions, sample and timeout must be positive")
    try:
        async with asyncio.timeout(timeout_seconds), uow_factory() as uow:
            names = await uow.blobs.list_ready_names(sample)
    except Exception as exc:
        logger.warning("Warm-up sampling failed: {}", type(exc).__name__)
        return {}
    if not names:
        return {}
    scope = SearchScope(frozenset(names))
    # Any unit vector reaches the index; the answer is discarded.
    vector = [1.0 / math.sqrt(dimensions)] * dimensions
    probes: list[tuple[str, Callable[[], Awaitable[object]]]] = [
        (
            "dense",
            lambda: search_store.search(
                query_vector=vector, allowed_blob_names=names, top_k=1
            ),
        )
    ]
    if path_store is not None:
        probes.append(
            (
                "path",
                lambda: path_store.search_paths(
                    query_vector=vector, allowed_blob_names=names, top_k=1
                ),
            )
        )
    if lexical_store is not None:
        probes.append(
            (
                "lexical",
                lambda: lexical_store.search_lexical(
                    terms=("warm",), phrases=(), scope=scope, top_k=1
                ),
            )
        )
    timings: dict[str, int] = {}
    for name, probe in probes:
        started = perf_counter()
        try:
            async with asyncio.timeout(timeout_seconds):
                await probe()
        except Exception as exc:
            logger.warning(
                "Warm-up of the {} store failed: {}", name, type(exc).__name__
            )
            continue
        timings[name] = int((perf_counter() - started) * 1000)
    logger.info("Retrieval stores warmed: {}", timings)
    return timings
