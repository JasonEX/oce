"""Opt-in internal timeout capture, separate from uninstrumented utility runs.

Set OCE_TIMEOUT_CAPTURE to a JSONL path and import before oce.cli.main().
No SQL parameters, raw queries, credentials, or source content are written.
"""

import asyncio
import hashlib
import json
import os
from contextvars import ContextVar
from pathlib import Path
from time import perf_counter

import oce.domain.services.retrieval.pipeline as retrieval

OUTPUT = Path(os.environ["OCE_TIMEOUT_CAPTURE"])
QUERY = ContextVar("timeout_query_hash", default=None)
LEXICAL = ContextVar("timeout_lexical_capture", default=None)
installed = False


def install():
    global installed
    if installed:
        return
    # Personal-mode CLI configures persistence before the first search.
    from sqlalchemy import event
    from sqlalchemy.engine import Engine
    from sqlalchemy.pool import QueuePool

    from oce.infrastructure.persistence.lexical_index import SqlLexicalSearchStore

    original_acquire = QueuePool._do_get

    def acquire(self):
        record = LEXICAL.get()
        if record is None:
            return original_acquire(self)
        start = perf_counter()
        try:
            return original_acquire(self)
        finally:
            # Includes connection creation if the pool has to open one.
            record["connection_acquisition_ms"].append((perf_counter() - start) * 1000)

    QueuePool._do_get = acquire

    @event.listens_for(Engine, "before_cursor_execute")
    def before(conn, cursor, statement, parameters, context, executemany):
        record = LEXICAL.get()
        if record is not None:
            row = {
                "statement_sha256": hashlib.sha256(statement.encode()).hexdigest(),
                "started_after_ms": (perf_counter() - record["_start"]) * 1000,
            }
            context._timeout_capture_sql = (perf_counter(), row)
            record["sql"].append(row)

    @event.listens_for(Engine, "after_cursor_execute")
    def after(conn, cursor, statement, parameters, context, executemany):
        measured = getattr(context, "_timeout_capture_sql", None)
        if measured is not None:
            start, row = measured
            row["completed_ms"] = (perf_counter() - start) * 1000

    def wrap(original, operation):
        async def lexical(self, *args, **kwargs):
            started = perf_counter()
            record = {
                "operation": operation,
                "query_sha256": QUERY.get(),
                "_start": started,
                "connection_acquisition_ms": [],
                "sql": [],
                "max_event_loop_delay_ms": 0.0,
            }
            token = LEXICAL.set(record)
            loop = asyncio.get_running_loop()
            expected = loop.time() + 0.02
            handle = None

            def tick():
                nonlocal expected, handle
                now = loop.time()
                record["max_event_loop_delay_ms"] = max(
                    record["max_event_loop_delay_ms"], max(0.0, now - expected) * 1000
                )
                expected = now + 0.02
                handle = loop.call_at(expected, tick)

            handle = loop.call_at(expected, tick)
            try:
                result = await original(self, *args, **kwargs)
                record["hits"] = len(result)
                return result
            except BaseException as exc:
                record["exception"] = type(exc).__name__
                raise
            finally:
                record["max_event_loop_delay_ms"] = max(
                    record["max_event_loop_delay_ms"],
                    max(0.0, loop.time() - expected) * 1000,
                )
                handle.cancel()
                record["elapsed_ms"] = (perf_counter() - record.pop("_start")) * 1000
                LEXICAL.reset(token)
                try:
                    with OUTPUT.open("a") as stream:
                        stream.write(json.dumps(record) + "\n")
                except OSError:
                    pass

        return lexical

    from oce.infrastructure.persistence.symbol_search_store import SymbolSearchStore

    SqlLexicalSearchStore.search_lexical = wrap(
        SqlLexicalSearchStore.search_lexical, "lexical"
    )
    SymbolSearchStore.search_exact = wrap(SymbolSearchStore.search_exact, "exact")
    installed = True


original_search = retrieval.RetrievalPipeline.search


async def search(self, query, *args, **kwargs):
    install()
    token = QUERY.set(hashlib.sha256(query.encode()).hexdigest())
    try:
        return await original_search(self, query, *args, **kwargs)
    finally:
        QUERY.reset(token)


retrieval.RetrievalPipeline.search = search
