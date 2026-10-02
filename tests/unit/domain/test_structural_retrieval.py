"""Structural SQL success avoids model work; fallbacks retain task ownership."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from oce.domain.services.retrieval import RetrievalPipeline
from oce.domain.services.search import DefinitionHit, SearchHit, SearchScope
from oce.shared.config.settings import RetrievalSettings
from oce.shared.metrics import RetrievalAudit
from tests.fakes.embedding import ControlledEmbedder
from tests.fakes.retrieval import (
    ControlledExactSearchStore,
    FakeEmbedder,
    FakeEvidenceStore,
    FakeExactSearchStore,
    FakePathContentStore,
    FakePathLookupStore,
    FakeSearchStore,
)

BLOB = "a" * 64
SCOPE = SearchScope(frozenset({BLOB}))


def _hit(path: str) -> SearchHit:
    return SearchHit(blob_name=BLOB, path=path, content="code", score=0.9)


def _settings(**overrides: object) -> RetrievalSettings:
    return RetrievalSettings(_env_file=None, relation_reserve_chars=0, **overrides)


async def test_symbol_sql_success_never_starts_the_model() -> None:
    embedder = ControlledEmbedder()
    exact = ControlledExactSearchStore([_hit("src/declaration.py")])
    dense = FakeSearchStore([_hit("src/semantic.py")])
    pipeline = RetrievalPipeline(
        embedder=embedder,
        store=dense,
        exact_store=exact,
        settings=_settings(),
    )
    audit = RetrievalAudit()
    task = asyncio.create_task(
        pipeline.search("Where is `target` defined?", SCOPE, audit=audit)
    )
    await asyncio.wait_for(exact.started.wait(), timeout=1.0)
    assert embedder.query_calls == []
    exact.release.set()

    hits = await asyncio.wait_for(task, timeout=1.0)

    assert [hit.path for hit in hits] == ["src/declaration.py"]
    assert embedder.query_calls == []
    assert dense.queries == []
    assert audit.dense_route == "skip:exact_definition"
    assert "embed" not in audit.stages and "dense" not in audit.stages


@pytest.mark.parametrize("error", [None, RuntimeError("SQL unavailable")])
async def test_symbol_sql_miss_or_failure_starts_embedding(
    error: Exception | None,
) -> None:
    embedder = ControlledEmbedder()
    pipeline = RetrievalPipeline(
        embedder=embedder,
        store=FakeSearchStore([_hit("src/semantic.py")]),
        exact_store=FakeExactSearchStore(error=error),
        settings=_settings(),
    )
    audit = RetrievalAudit()
    task = asyncio.create_task(
        pipeline.search("Where is `target` defined?", SCOPE, audit=audit)
    )
    response = await asyncio.wait_for(embedder.requests.get(), timeout=1.0)
    response.set_result([1.0])

    hits = await asyncio.wait_for(task, timeout=1.0)

    assert [hit.path for hit in hits] == ["src/semantic.py"]
    assert embedder.query_calls == ["Where is `target` defined?"]
    assert audit.dense_route == "dense"
    assert "embed" in audit.stages and "dense" in audit.stages
    assert audit.lane_failures == ({"exact": "RuntimeError"} if error else {})


async def test_path_sql_success_never_starts_the_model() -> None:
    embedder = FakeEmbedder()
    dense = FakeSearchStore([_hit("src/semantic.py")])
    content = FakePathContentStore([_hit("src/target.py")])
    pipeline = RetrievalPipeline(
        embedder=embedder,
        store=dense,
        path_lookup_store=FakePathLookupStore({BLOB: 1.0}),
        path_content_store=content,
        settings=_settings(),
    )
    audit = RetrievalAudit()

    hits = await pipeline.search("Where is src/target.py?", SCOPE, audit=audit)

    assert [hit.path for hit in hits] == ["src/target.py"]
    assert embedder.queries == []
    assert dense.queries == []
    assert content.blob_names == (BLOB,)
    assert audit.dense_route == "skip:path_evidence"


@pytest.mark.parametrize("content_available", [False, True])
async def test_path_needs_vectors_when_sql_cannot_supply_content(
    content_available: bool,
) -> None:
    embedder = FakeEmbedder()
    dense = FakeSearchStore([_hit("src/target.py")])
    pipeline = RetrievalPipeline(
        embedder=embedder,
        store=dense,
        path_lookup_store=FakePathLookupStore({} if content_available else {BLOB: 1.0}),
        path_content_store=FakePathContentStore() if content_available else None,
        settings=_settings(),
    )
    audit = RetrievalAudit()

    hits = await pipeline.search("Where is src/target.py?", SCOPE, audit=audit)

    assert [hit.path for hit in hits] == ["src/target.py"]
    assert embedder.queries == ["Where is src/target.py?"]
    assert dense.queries == embedder.queries
    assert audit.dense_route == "dense"


async def test_disabled_decisive_skip_keeps_sql_and_embedding_parallel() -> None:
    embedder = ControlledEmbedder()
    exact = ControlledExactSearchStore([_hit("src/declaration.py")])
    pipeline = RetrievalPipeline(
        embedder=embedder,
        store=FakeSearchStore([_hit("src/semantic.py")]),
        exact_store=exact,
        settings=_settings(decisive_skips_dense=False),
    )
    task = asyncio.create_task(pipeline.search("Where is `target` defined?", SCOPE))
    response = await asyncio.wait_for(embedder.requests.get(), timeout=1.0)
    assert exact.started.is_set()
    assert not exact.release.is_set()
    response.set_result([1.0])
    exact.release.set()

    hits = await asyncio.wait_for(task, timeout=1.0)

    assert [hit.path for hit in hits] == ["src/declaration.py", "src/semantic.py"]


async def test_reference_sql_success_releases_an_already_sent_embedding() -> None:
    embedder = ControlledEmbedder()
    exact = ControlledExactSearchStore(
        [_hit("src/declaration.py")], use_sites=[_hit("src/caller.py")]
    )
    pipeline = RetrievalPipeline(
        embedder=embedder,
        store=FakeSearchStore(),
        exact_store=exact,
        settings=_settings(),
    )
    audit = RetrievalAudit()
    task = asyncio.create_task(
        pipeline.search("Which modules use `target`?", SCOPE, audit=audit)
    )
    response = await asyncio.wait_for(embedder.requests.get(), timeout=1.0)
    assert exact.started.is_set() and not exact.release.is_set()
    exact.release.set()

    hits = await asyncio.wait_for(task, timeout=1.0)

    assert hits
    assert audit.dense_route == "skip:use_sites"
    assert not response.done()
    assert embedder.cancelled_calls == 0
    response.set_result([1.0])
    await asyncio.sleep(0)


async def test_cancelling_before_sql_finishes_does_not_start_embedding() -> None:
    embedder = ControlledEmbedder()
    exact = ControlledExactSearchStore()
    pipeline = RetrievalPipeline(
        embedder=embedder,
        store=FakeSearchStore(),
        exact_store=exact,
        settings=_settings(),
    )
    task = asyncio.create_task(pipeline.search("Where is `target` defined?", SCOPE))
    await asyncio.wait_for(exact.started.wait(), timeout=1.0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert exact.finished.is_set()
    assert embedder.query_calls == []


async def test_cancelling_after_sql_miss_releases_embedding_waiter() -> None:
    embedder = ControlledEmbedder()
    pipeline = RetrievalPipeline(
        embedder=embedder,
        store=FakeSearchStore(),
        exact_store=FakeExactSearchStore(),
        settings=_settings(),
    )
    task = asyncio.create_task(pipeline.search("Where is `target` defined?", SCOPE))
    response = await asyncio.wait_for(embedder.requests.get(), timeout=1.0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert not response.done()
    assert embedder.cancelled_calls == 0
    response.set_result([1.0])
    await asyncio.sleep(0)


async def test_same_file_call_chain_endpoints_reach_primary_selection() -> None:
    entry = replace(
        _hit("src/flow.py"),
        content="def begin_operation(): pass",
        start_line=1,
        end_line=5,
        content_hash="entry",
    )
    target = replace(
        _hit("src/flow.py"),
        content="def finish_operation(): pass",
        start_line=30,
        end_line=35,
        content_hash="target",
    )
    unrelated = replace(_hit("src/unrelated.py"), blob_name="b" * 64)
    exact = FakeEvidenceStore(
        [
            DefinitionHit("begin_operation", "definition", entry, 1, 5),
            DefinitionHit("finish_operation", "definition", target, 30, 35),
        ]
    )
    pipeline = RetrievalPipeline(
        embedder=FakeEmbedder(),
        store=FakeSearchStore([unrelated]),
        exact_store=exact,
        settings=_settings(final_select_k=2, merge_adjacent_enabled=False),
    )
    audit = RetrievalAudit()

    hits = await pipeline.search(
        "How does `begin_operation` reach `finish_operation`?",
        SearchScope(frozenset({BLOB, unrelated.blob_name})),
        audit=audit,
    )

    assert audit.intent == "call_chain"
    assert audit.head_slots == 2
    assert [hit.content_hash for hit in hits if hit.role == "primary"] == [
        "entry",
        "target",
    ]
