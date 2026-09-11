"""Kinds, frequency damping, and definition lookup over symbol_occurrences."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from oce.application.call_chain import resolve_endpoints
from oce.domain.blob.blob import Blob, BlobStatus
from oce.domain.chunk import Chunk
from oce.domain.services.search import SearchScope
from oce.infrastructure.astchunk.symbol_provider import TreeSitterSymbolProvider
from oce.infrastructure.persistence.models import BlobModel, SymbolOccurrenceModel
from oce.infrastructure.persistence.sql_blob_repo import SqlBlobRepository
from oce.infrastructure.persistence.sql_chain_repo import SqlChainRepository
from oce.infrastructure.persistence.sql_chunk_repo import SqlChunkRepository
from oce.infrastructure.persistence.sql_symbol_projection import SqlSymbolProjection
from oce.infrastructure.persistence.symbol_search_store import SymbolSearchStore
from oce.infrastructure.regex_symbol_provider import RegexSymbolProvider
from oce.shared.database.session import Base
from tests.conftest import make_sha256


@pytest.fixture
async def sessions():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _index_files(session: AsyncSession, files: dict[str, str]) -> dict[str, str]:
    """Index each file as two line-based chunks so definitions cross chunks."""
    projection = SqlSymbolProjection(
        session, TreeSitterSymbolProvider(RegexSymbolProvider())
    )
    chunk_repo = SqlChunkRepository(session)
    blob_repo = SqlBlobRepository(session)
    names: dict[str, str] = {}
    for path, content in files.items():
        lines = content.splitlines()
        half = max(1, len(lines) // 2)
        chunks = []
        for start, end in ((1, half), (half + 1, len(lines))):
            if end < start:
                continue
            text = "\n".join(lines[start - 1 : end])
            chunks.append(
                Chunk(
                    Chunk.compute_hash(text),
                    path,
                    text,
                    start,
                    end,
                    context=f"ctx:{path}",
                )
            )
        await chunk_repo.save_many(chunks)
        name = make_sha256(path)
        blob = Blob(
            name,
            path,
            BlobStatus.READY,
            chunks=[c.to_ref() for c in chunks],
            language="python",
        )
        await blob_repo.save(blob)
        await projection.index(blob, chunks, content)
        names[path] = name
    await session.commit()
    return names


_FILES = {
    "src/base.py": "import os\n\n\nclass BaseService:\n    limit = 3\n\n    def start(self):\n        return self.limit\n",
    "src/svc.py": "from src.base import BaseService\n\n\nclass Service(BaseService):\n    def run(self):\n        return start_all()\n",
    "src/util.py": "def start_all():\n    return 1\n\n\ndef helper():\n    return 2\n\n\ndef helper():\n    return 3\n",
}


@pytest.mark.parametrize("operation", ["search_exact", "find_definitions"])
async def test_small_scope_definition_lookup_has_bounded_sql_work(sessions, operation):
    identifiers = [f"operation_{index}" for index in range(40)]
    content = "\n\n".join(f"def {name}():\n    return 1" for name in identifiers)
    async with sessions() as session:
        names = await _index_files(session, {"src/operations.py": content})
        chain = await SqlChainRepository(session).create(list(names.values()))
        # Ready files in other projects must not cause every requested name
        # to be probed in every unrelated file before scope is checked.
        await session.execute(
            insert(BlobModel),
            [
                {
                    "blob_name": make_sha256(f"unrelated-{index}"),
                    "path": f"other/{index}.py",
                    "content_size": 0,
                    "status": BlobStatus.READY.value,
                }
                for index in range(2000)
            ],
        )
        await session.commit()
        connection = await session.connection()
        raw = await connection.get_raw_connection()
        driver = raw.driver_connection

    work = 0

    def within_budget():
        nonlocal work
        work += 1000
        return int(work > 200_000)

    # SQLite instruction counts avoid a machine-speed-dependent latency test.
    await driver.set_progress_handler(within_budget, 1000)
    try:
        result = await getattr(SymbolSearchStore(sessions), operation)(
            identifiers=identifiers,
            scope=SearchScope(
                frozenset(names.values()),
                chain_id=chain.chain_id,
                chain_version=chain.version,
            ),
        )
    finally:
        await driver.set_progress_handler(None, 1000)
    assert result
    if operation == "find_definitions":
        assert [item.identifier for item in result] == identifiers
    else:
        assert {hit.path for hit in result} == {"src/operations.py"}


@pytest.mark.parametrize(
    ("operation", "kwargs", "empty"),
    [
        ("search_exact", {"identifiers": ["work"]}, []),
        ("find_definitions", {"identifiers": ["work"]}, []),
        ("find_callers", {"identifiers": ["work"]}, []),
        ("defined_identifiers", {"occurrences": [("blob", "chunk")]}, {}),
        ("calls_within", {"blob_name": "blob", "start_line": 1, "end_line": 2}, []),
        ("chunk_for_line", {"blob_name": "blob", "line": 1}, None),
    ],
)
async def test_symbol_timeout_preserves_fallback_and_reports_missing_evidence(
    monkeypatch, operation, kwargs, empty
):
    from loguru import logger

    messages = []
    monkeypatch.setattr(logger, "warning", messages.append)

    @asynccontextmanager
    async def blocked_session():
        await asyncio.Event().wait()
        yield

    store = SymbolSearchStore(blocked_session, timeout_seconds=0.001)
    result = await getattr(store, operation)(
        scope=SearchScope(frozenset({"blob"})), **kwargs
    )
    assert result == empty
    assert len(messages) == 1
    assert "timed out" in messages[0]


async def test_large_file_projection_obeys_sqlite_bind_limit_and_is_idempotent(
    sessions,
):
    content = "\n\n".join(
        f"def symbol_{index}():\n    return {index}" for index in range(400)
    )
    async with sessions() as session:
        connection = await session.connection()
        raw = await connection.get_raw_connection()
        driver = raw.driver_connection
        # The SQLite connection belongs to aiosqlite's worker thread. Exercise
        # the historical 999-variable limit independently of this host's build.
        await driver._execute(
            driver._conn.setlimit, sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999
        )
        names = await _index_files(session, {"src/large.py": content})
        await _index_files(session, {"src/large.py": content})
        assert (
            await session.scalar(
                select(func.count()).select_from(SymbolOccurrenceModel)
            )
            == 400
        )

    definitions = await SymbolSearchStore(sessions).find_definitions(
        identifiers=["symbol_0", "symbol_399"],
        scope=SearchScope(frozenset(names.values())),
    )
    assert [item.identifier for item in definitions] == ["symbol_0", "symbol_399"]


async def test_kinds_filter_and_chunk_span_alignment(sessions):
    async with sessions() as session:
        names = await _index_files(session, _FILES)
    store = SymbolSearchStore(sessions)
    scope = SearchScope(frozenset(names.values()))

    definitions = await store.search_exact(
        identifiers=["BaseService"], scope=scope, kinds=("endpoint", "definition")
    )
    assert [hit.path for hit in definitions] == ["src/base.py"]
    # Hit lines are the chunk's lines, not the definition's, so the formatter
    # prints the content at the right numbers; context rides along.
    assert definitions[0].start_line == 1
    assert definitions[0].context == "ctx:src/base.py"

    everything = await store.search_exact(identifiers=["BaseService"], scope=scope)
    assert {hit.path for hit in everything} == {"src/base.py", "src/svc.py"}
    # The import occurrence ranks below the definition.
    assert everything[0].path == "src/base.py"


async def test_frequency_damping_and_definition_lookup(sessions):
    async with sessions() as session:
        names = await _index_files(session, _FILES)
    store = SymbolSearchStore(sessions)
    scope = SearchScope(frozenset(names.values()))

    hits = await store.search_exact(identifiers=["helper", "start_all"], scope=scope)
    by_path_start = {(hit.path, hit.start_line): hit.score for hit in hits}
    # ``helper`` is defined twice, so each hit carries less evidence than the
    # single ``start_all`` definition.
    assert by_path_start[("src/util.py", 1)] > max(
        score for (path, start), score in by_path_start.items() if start != 1
    )

    definitions = await store.find_definitions(
        identifiers=["start_all", "BaseService", "helper", "missing"],
        scope=scope,
        max_per_identifier=1,
    )
    found = [(d.identifier, d.hit.path, d.start_line, d.end_line) for d in definitions]
    assert ("start_all", "src/util.py", 1, 2) in found
    assert ("BaseService", "src/base.py", 4, 8) in found
    # Two definitions exceed max_per_identifier=1; unknown names are absent.
    assert not any(item[0] in {"helper", "missing"} for item in found)
    # Ordered by the caller's identifier order.
    assert [d.identifier for d in definitions] == ["start_all", "BaseService"]


async def test_calls_are_exact_reference_evidence_without_definition_damping(sessions):
    files = {
        "src/api.py": (
            "from src.invoice import build_invoice\n"
            "def create(request):\n"
            "    total = build_invoice(request)\n"
            "    return apply_discount(total, 10)\n"
            "\n"
            "def other():\n"
            "    return len([])\n"
        ),
        "src/invoice.py": "def build_invoice(r):\n    return 1\n\ndef apply_discount(t, p):\n    return t\n",
    }
    async with sessions() as session:
        names = await _index_files(session, files)
    store = SymbolSearchStore(sessions)
    scope = SearchScope(frozenset(names.values()))
    call_hits = await store.search_exact(
        identifiers=["build_invoice", "apply_discount", "len"],
        scope=scope,
        kinds=("call",),
    )
    assert {hit.path for hit in call_hits} == {"src/api.py"}
    assert len(call_hits) == 2
    assert all(hit.score == 0.9 for hit in call_hits)

    hits = await store.search_exact(
        identifiers=["build_invoice", "apply_discount"], scope=scope
    )
    # A definition called from many places is still one unambiguous definition.
    definition = next(
        hit
        for hit in hits
        if hit.path == "src/invoice.py" and "def build_invoice" in hit.content
    )
    assert definition.score == 0.95


async def test_usage_evidence_is_diversified_per_file_before_the_window_closes(
    sessions,
):
    # One test module calls the symbol in both of its chunks; the imports in
    # the other files must still fit inside a two-hit window.
    files = {
        "tests/test_pool.py": (
            "from src.pool import acquire\n"
            "def test_a():\n"
            "    acquire()\n"
            "    acquire()\n"
            "def test_b():\n"
            "    acquire()\n"
            "    acquire()\n"
        ),
        "src/server.py": "from src.pool import acquire\n\n\ndef serve():\n    return 1\n",
        "src/pool.py": "def acquire():\n    return 1\n\n\ndef release():\n    return 2\n",
    }
    async with sessions() as session:
        names = await _index_files(session, files)
    store = SymbolSearchStore(sessions)
    scope = SearchScope(frozenset(names.values()))
    hits = await store.search_exact(identifiers=["acquire"], scope=scope, top_k=3)
    paths = [hit.path for hit in hits]
    # Definition first, then the best chunk of each using file; the second
    # test chunk waits behind the server import.
    assert paths[0] == "src/pool.py"
    assert set(paths[1:3]) == {"tests/test_pool.py", "src/server.py"}

    kinds = await store.occurrence_kinds(
        [(hit.blob_name, hit.content_hash) for hit in hits] + [("missing", "missing")],
        scope,
    )
    assert kinds[(hits[0].blob_name, hits[0].content_hash)] == frozenset({"definition"})
    server = next(hit for hit in hits if hit.path == "src/server.py")
    assert kinds[(server.blob_name, server.content_hash)] == frozenset({"import"})
    assert ("missing", "missing") not in kinds


async def test_occurrence_kinds_is_scoped_and_disambiguates_duplicate_chunks(sessions):
    shared = "from package import helper\n"
    files = {
        "src/importer.py": shared,
        "src/definition.py": shared,
        "vendor/importer.py": shared,
    }
    async with sessions() as session:
        names = await _index_files(session, files)
        shared_hash = Chunk.compute_hash(shared.rstrip("\n"))
        session.add_all(
            [
                SymbolOccurrenceModel(
                    identifier="helper",
                    blob_name=names["src/definition.py"],
                    content_hash=shared_hash,
                    kind="definition",
                    start_line=1,
                    end_line=1,
                ),
                SymbolOccurrenceModel(
                    identifier="helper",
                    blob_name=names["vendor/importer.py"],
                    content_hash=shared_hash,
                    kind="definition",
                    start_line=1,
                    end_line=1,
                ),
            ]
        )
        await session.commit()

    scope = SearchScope(
        frozenset({names["src/importer.py"], names["src/definition.py"]})
    )
    kinds = await SymbolSearchStore(sessions).occurrence_kinds(
        [
            (names["src/importer.py"], shared_hash),
            (names["src/definition.py"], shared_hash),
            (names["vendor/importer.py"], shared_hash),
        ],
        scope,
    )
    assert kinds[(names["src/importer.py"], shared_hash)] == frozenset({"import"})
    assert kinds[(names["src/definition.py"], shared_hash)] == frozenset(
        {"import", "definition"}
    )
    assert (names["vendor/importer.py"], shared_hash) not in kinds


async def test_relation_lookups_group_by_enclosing_and_prefer_source(sessions):
    files = {
        "src/billing/__init__.py": "from billing.invoice import build_invoice\n",
        "src/billing/invoice.py": (
            "class Builder:\n"
            "    def add(self):\n"
            "        return 1\n"
            "\n"
            "\n"
            "def build_invoice(customer):\n"
            "    return Builder().add()\n"
        ),
        "src/billing/api.py": (
            "from billing.invoice import build_invoice\n"
            "\n"
            "\n"
            "def create(request):\n"
            "    first = build_invoice(request.customer)\n"
            "    second = build_invoice(request.other)\n"
            "    return first, second\n"
            "\n"
            "\n"
            "def preview(request):\n"
            "    return build_invoice(request.customer)\n"
        ),
        "src/billing/plugins.py": "from billing.invoice import Builder\n\n\nclass FancyBuilder(Builder):\n    pass\n",
        "tests/test_invoice.py": (
            "from billing.invoice import build_invoice\n"
            "\n"
            "\n"
            "def test_totals():\n"
            "    assert build_invoice(None)\n"
            "\n"
            "\n"
            "def test_other():\n"
            "    assert build_invoice(1)\n"
        ),
    }
    async with sessions() as session:
        names = await _index_files(session, files)
    store = SymbolSearchStore(sessions)
    scope = SearchScope(frozenset(names.values()))

    callers = await store.find_callers(
        identifiers=["build_invoice"], scope=scope, limit=8
    )
    # One edge per (file, enclosing function): the two calls inside ``create``
    # collapse; test files have their own section and are not callers.
    assert [(c.hit.path, c.enclosing) for c in callers] == [
        ("src/billing/api.py", "create"),
        ("src/billing/api.py", "preview"),
    ]
    assert callers[0].kind == "call" and callers[0].line == 5

    tests = await store.find_test_uses(
        identifiers=["build_invoice"], scope=scope, limit=3
    )
    assert [(t.hit.path, t.enclosing) for t in tests][0] == (
        "tests/test_invoice.py",
        "",
    )
    assert all(t.hit.path.startswith("tests/") for t in tests)

    implementations = await store.find_implementations(
        identifiers=["Builder"], scope=scope, limit=4
    )
    assert [(i.hit.path, i.enclosing) for i in implementations] == [
        ("src/billing/plugins.py", "FancyBuilder")
    ]

    reexports = await store.find_reexports(identifiers=["build_invoice"], scope=scope)
    assert [(r.hit.path, r.line) for r in reexports] == [("src/billing/__init__.py", 1)]

    definitions = await store.search_exact(
        identifiers=["build_invoice"], scope=scope, kinds=("definition",)
    )
    defined = await store.defined_identifiers(
        [(hit.blob_name, hit.content_hash) for hit in definitions], scope
    )
    assert set().union(*defined.values()) == {"build_invoice"}


async def test_test_relation_prefilter_keeps_benchmark_directories(sessions):
    files = {
        "src/worker.py": "def run_job():\n    return 1\n",
        "benchmarks/worker_bench.py": (
            "from src.worker import run_job\n"
            "def benchmark_run():\n"
            "    return run_job()\n"
        ),
    }
    async with sessions() as session:
        names = await _index_files(session, files)
    store = SymbolSearchStore(sessions)
    scope = SearchScope(frozenset(names.values()))

    tests = await store.find_test_uses(identifiers=["run_job"], scope=scope, limit=4)

    assert tests
    assert all(item.hit.path == "benchmarks/worker_bench.py" for item in tests)
    assert any(item.enclosing == "benchmark_run" for item in tests)


async def test_caller_filter_keeps_source_paths_containing_test_or_spec(sessions):
    files = {
        "src/worker.py": "def run_job():\n    return 1\n",
        "src/contest/runner.py": (
            "from src.worker import run_job\ndef run_contest():\n    return run_job()\n"
        ),
        "src/special/runner.py": (
            "from src.worker import run_job\ndef run_special():\n    return run_job()\n"
        ),
    }
    async with sessions() as session:
        names = await _index_files(session, files)
    store = SymbolSearchStore(sessions)
    scope = SearchScope(frozenset(names.values()))

    callers = await store.find_callers(identifiers=["run_job"], scope=scope, limit=4)

    assert [(item.hit.path, item.enclosing) for item in callers] == [
        ("src/contest/runner.py", "run_contest"),
        ("src/special/runner.py", "run_special"),
    ]


async def test_relation_queries_diversify_large_scopes_before_global_limit(sessions):
    files = {"src/worker.py": "def run_job():\n    return 1\n"}
    files.update(
        {
            f"src/caller_{index}.py": (
                "from src.worker import run_job\n"
                f"def caller_{index}():\n"
                "    return run_job()\n"
            )
            for index in range(12)
        }
    )
    files.update(
        {
            f"tests/test_worker_{index}.py": (
                "from src.worker import run_job\n"
                f"def test_worker_{index}():\n"
                "    return run_job()\n"
            )
            for index in range(12)
        }
    )
    async with sessions() as session:
        names = await _index_files(session, files)
    store = SymbolSearchStore(sessions)
    scope = SearchScope(frozenset(names.values()))

    callers = await store.find_callers(identifiers=["run_job"], scope=scope, limit=8)
    tests = await store.find_test_uses(identifiers=["run_job"], scope=scope, limit=8)

    assert len({item.hit.path for item in callers}) == 8
    assert all(item.hit.path.startswith("src/caller_") for item in callers)
    assert len({item.hit.path for item in tests}) == 8
    assert all(item.hit.path.startswith("tests/test_worker_") for item in tests)


async def test_calls_within_lists_the_calls_of_one_span_with_their_enclosing(sessions):
    files = {
        "src/api.py": (
            "from src.invoice import build_invoice\n"
            "class Handler:\n"
            "    def create(self, request):\n"
            "        total = build_invoice(request)\n"
            "        return apply_discount(total, 10)\n"
            "\n"
            "    def other(self):\n"
            "        return build_invoice(None)\n"
        ),
        "src/invoice.py": "def build_invoice(r):\n    return 1\n\ndef apply_discount(t, p):\n    return t\n",
    }
    async with sessions() as session:
        names = await _index_files(session, files)
    store = SymbolSearchStore(sessions)
    scope = SearchScope(frozenset(names.values()))
    # The whole class span: each called name once, in line order, with the
    # method that makes the call.
    calls = await store.calls_within(
        blob_name=names["src/api.py"], start_line=2, end_line=8, scope=scope
    )
    assert calls == [("build_invoice", 4, "create"), ("apply_discount", 5, "create")]
    # Only the second method: the first call to build_invoice is outside.
    calls = await store.calls_within(
        blob_name=names["src/api.py"], start_line=7, end_line=8, scope=scope
    )
    assert calls == [("build_invoice", 8, "other")]
    # Out of scope blobs return nothing.
    assert (
        await store.calls_within(
            blob_name=names["src/api.py"],
            start_line=1,
            end_line=8,
            scope=SearchScope(frozenset({names["src/invoice.py"]})),
        )
        == []
    )


async def test_chunk_for_line_returns_the_chunk_spanning_the_line(sessions):
    files = {
        "src/api.py": (
            "from src.invoice import build_invoice\n"
            "class Handler:\n"
            "    def create(self, request):\n"
            "        total = build_invoice(request)\n"
            "        return apply_discount(total, 10)\n"
            "\n"
            "    def other(self):\n"
            "        return build_invoice(None)\n"
        ),
    }
    async with sessions() as session:
        names = await _index_files(session, files)
    store = SymbolSearchStore(sessions)
    scope = SearchScope(frozenset(names.values()))
    # Files are indexed as two line-based halves: lines 1-4 and 5-8.
    first = await store.chunk_for_line(
        blob_name=names["src/api.py"], line=2, scope=scope
    )
    second = await store.chunk_for_line(
        blob_name=names["src/api.py"], line=7, scope=scope
    )
    assert first is not None and (first.start_line, first.end_line) == (1, 4)
    assert second is not None and (second.start_line, second.end_line) == (5, 8)
    assert second.path == "src/api.py" and "def other" in second.content
    assert (
        await store.chunk_for_line(blob_name=names["src/api.py"], line=99, scope=scope)
        is None
    )
    assert (
        await store.chunk_for_line(
            blob_name=names["src/api.py"], line=2, scope=SearchScope(frozenset())
        )
        is None
    )


async def test_find_definitions_pinned_to_an_enclosing_definition(sessions):
    files = {
        "src/router.py": "class Router:\n    def route(self):\n        return 1\n",
        "src/resource.py": "class Resource:\n    def route(self):\n        return 2\n",
        "src/free.py": "def route():\n    return 3\n",
    }
    async with sessions() as session:
        names = await _index_files(session, files)
    store = SymbolSearchStore(sessions)
    scope = SearchScope(frozenset(names.values()))
    # Three declarations exceed the cap; the pin counts only Router's.
    assert (
        await store.find_definitions(
            identifiers=["route"], scope=scope, max_per_identifier=1
        )
        == []
    )
    pinned = await store.find_definitions(
        identifiers=["route"], scope=scope, max_per_identifier=1, enclosing=["Router"]
    )
    assert [(d.hit.path, d.enclosing) for d in pinned] == [("src/router.py", "Router")]
    assert (
        await store.find_definitions(
            identifiers=["route"],
            scope=SearchScope(frozenset({names["src/resource.py"]})),
            max_per_identifier=1,
            enclosing=["Router"],
        )
        == []
    )
    assert (
        await store.find_definitions(
            identifiers=["route"],
            scope=scope,
            max_per_identifier=3,
            enclosing=["Missing"],
        )
        == []
    )


async def test_qualified_endpoints_survive_scope_wide_homonyms(sessions):
    files = {
        "src/gate.py": "class Gate:\n    def enter_request(self):\n        return 1\n",
        "src/sink.py": "class Sink:\n    def handle_request(self):\n        return 2\n",
        "src/others.py": "\n".join(
            f"class Other{i}:\n"
            "    def enter_request(self):\n        return 3\n"
            "    def handle_request(self):\n        return 4\n"
            for i in range(41)
        ),
    }
    async with sessions() as session:
        names = await _index_files(session, files)
    store = SymbolSearchStore(sessions)
    scope = SearchScope(frozenset(names.values()))
    # Both leaves exceed the old wide lookup cap, but each qualified
    # endpoint has one declaration. Exercise the pipeline and real SQL
    # together so neither a mock nor an unqualified lookup can hide the loss.
    assert (
        await store.find_definitions(
            identifiers=("enter_request", "handle_request"),
            scope=scope,
            max_per_identifier=40,
        )
        == []
    )
    endpoints = await resolve_endpoints(
        store,
        scope,
        ("Gate.enter_request", "Sink.handle_request"),
        {"enter_request": ("Gate",), "handle_request": ("Sink",)},
    )
    assert [
        (leaf, [(hit.hit.path, hit.enclosing) for hit in hits])
        for leaf, hits in endpoints
    ] == [
        ("enter_request", [("src/gate.py", "Gate")]),
        ("handle_request", [("src/sink.py", "Sink")]),
    ]
