"""Deterministic retrieval dump for proving that a refactor preserved behaviour.

The dump indexes a directory of source files into an in-memory SQLite database
through the real indexing pipeline (tree-sitter symbols, the lexical index,
path lookup), replaces the two embedding-backed stores with deterministic
term-frequency fakes, and runs a fixed query list under several settings
profiles through ``RetrievalPipeline``. Every returned excerpt and every audit
field that the pipeline decides is written out; two dumps taken before and
after a code change must be identical for the change to count as pure.

Usage::

    python -m benchmarks.internal.retrieval_equivalence dump --out before.json
    python -m benchmarks.internal.retrieval_equivalence dump --out after.json
    python -m benchmarks.internal.retrieval_equivalence compare before.json after.json

``--corpus`` defaults to ``src/oce``; ``--queries`` accepts a JSON list of
strings to replace the built-in list. Dense recall is a fake, so the dump
proves that two revisions of the pipeline compute the same function of the
same inputs; it says nothing about product utility.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from oce.application.service import compute_blob_name
from oce.domain.services.indexing import IndexingPipeline
from oce.domain.services.path_search import PathSearchResult
from oce.domain.services.retrieval import RetrievalPipeline
from oce.domain.services.search import SearchHit, SearchScope, VectorRecord
from oce.infrastructure.astchunk.symbol_provider import TreeSitterSymbolProvider
from oce.infrastructure.chunkers.factory import build_chunker
from oce.infrastructure.persistence.lexical_index import (
    SqlLexicalSearchStore,
    create_lexical_table,
)
from oce.infrastructure.persistence.path_content_store import SqlPathContentStore
from oce.infrastructure.persistence.path_lookup_store import SqlPathLookupStore
from oce.infrastructure.persistence.symbol_search_store import SymbolSearchStore
from oce.infrastructure.persistence.uow import SqlAlchemyUnitOfWork
from oce.infrastructure.regex_symbol_provider import RegexSymbolProvider
from oce.shared.config.settings import RetrievalSettings
from oce.shared.database.session import Base
from oce.shared.metrics import RetrievalAudit

DIMENSIONS = 128
_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,}")

# Settings overrides per profile; ``reranker`` adds a deterministic fake
# cross-encoder so the rerank routes and the head rules that run after a model
# reorder the list are exercised too.
PROFILES: dict[str, dict[str, Any]] = {
    "default": {},
    "hubs": {"hub_head_slots": 2},
    "no_decisive": {"decisive_skips_dense": False},
    "ambiguous_rerank": {"rerank_ambiguous_definitions": True, "source_head_slots": 5},
    "focused_small": {
        "focused_max_context_chars": 3_000,
        "max_context_chars": 6_000,
        "relation_reserve_chars": 1_500,
        "call_chain_max_hops": 2,
    },
    "reranker": {"reranker": True, "rerank_policy": "adaptive"},
    "reranker_always": {"reranker": True, "rerank_policy": "always"},
}

QUERIES: tuple[str, ...] = (
    "Where is `RetrievalPipeline` defined?",
    "Where is `RetrievalPipeline.search` defined?",
    "`resolve_qualified_hits` 函数在哪里定义？",
    "Where is `plan_rerank(intent, candidate_count)` defined?",
    "Where is the `SearchHit` class defined?",
    "Where is `SqlAlchemyUnitOfWork.__aenter__` defined?",
    "`build_index_profile` 在哪里定义",
    "Where is `verify_admin_key` defined?",
    "Where is `Milvus3Client` defined?",
    "Where is `PeriodicTask` defined?",
    "Where is the `Chunker` protocol defined?",
    "Where is `SwappableDelegate` defined?",
    "Where is settings.py?",
    "retrieval_strategy.py 文件在哪里",
    "Show me the symbol_search_store.py file",
    "container.py",
    "Where is `search_hit_key` used?",
    "Which functions call `resolve_qualified_hits`?",
    "哪些地方调用了 `plan_rerank`？",
    "Which modules import `RetrievalAudit`?",
    "Which tests cover `merge_adjacent_hits`?",
    "Which classes implement `PeriodicTask`?",
    "Where is `Reranker` implemented for `CredentialConfiguredReranker`?",
    "How does `RetrievalPipeline.search` reach `assemble_sections`?",
    "How does `batch_upload` reach `embed_pending`?",
    "Trace how `IngestBlobsCommandHandler.handle` dispatches a request.",
    "How does the request reach the embedding client from the retrieval pipeline?",
    "How does rerank policy routing decide whether to call the LLM reranker?",
    "Where is the confidence floor applied to candidates?",
    "How are query vectors cached between retrievals?",
    "How does the worker retry a failed embedding batch?",
    "Where do we resolve the active credential for a model kind?",
    "Explain the retrieval state machine and its stages",
    "Overview of the indexing pipeline and how chunks reach Milvus",
    "How is the monitoring subsystem structured?",
    "Explain the request context lifecycle for a codebase retrieval",
    "Explain the checkpoint chain and how scopes are resolved",
    (
        "IndexProfile mismatch after reload\n"
        "\n"
        "After changing EMBED_MODEL the server fails on startup.\n"
        "\n"
        "Traceback (most recent call last):\n"
        '  File "/app/src/oce/main.py", line 36, in lifespan\n'
        "    await container.ensure_index_compatible()\n"
        '  File "/app/src/oce/application/container.py", line 512, in ensure_index_compatible\n'
        "    await self.embedding_runtime.validate_prepared(replacement)\n"
        '  File "/app/src/oce/infrastructure/embed/credential_embedder.py", line 185, in validate_prepared\n'
        "    await self._validate_index_profile(replacement.config)\n"
        '  File "/app/src/oce/application/index_lifecycle.py", line 140, in ensure_compatible\n'
        "    self._assert_match(stored, current)\n"
        "ServiceNotReadyError: Index profile mismatch (stored=abc, current=def; changed=embedding.model)\n"
    ),
    (
        "Lexical recall times out on large scopes\n"
        "\n"
        '`SqlLexicalSearchStore.search_lexical` logs "Lexical recall timed out; using other candidates" '
        "for a 5000-blob checkpoint; `run_scoped` expands the scope into batches. "
        "Expected the relational predicate to apply instead of `IN (...)`."
    ),
    (
        "Queue reset refuses while worker is running\n"
        "\n"
        "Calling POST /admin/queue/reset returns 409 QUEUE_BUSY even though the worker was stopped. "
        "ResetQueueCommandHandler checks worker_running before touching Redis."
    ),
    "path lookup suffix matching and traceback frames",
    "hash-based blob naming for uploads",
    "lru cache for query vectors ttl",
)


def _tokens(text: str) -> list[str]:
    return [match.group().lower() for match in _TOKEN.finditer(text)]


def _term_vector(text: str) -> list[float]:
    """Deterministic bag-of-hashed-tokens vector, unit length."""
    vector = [0.0] * DIMENSIONS
    for token in _tokens(text):
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=4).digest()
        vector[int.from_bytes(digest, "big") % DIMENSIONS] += 1.0
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm else vector


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


class PathOrderReranker:
    """Candidate-preserving fake reranker: deterministic order by path hash."""

    async def rerank(self, query: str, hits: list[SearchHit]) -> list[SearchHit]:
        seed = hashlib.sha256(query.encode("utf-8")).hexdigest()[:8]
        return sorted(
            hits,
            key=lambda hit: hashlib.sha256(
                f"{seed}:{hit.path}:{hit.start_line}".encode()
            ).hexdigest(),
        )

    async def close(self) -> None:
        return None


class TermEmbedder:
    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [_term_vector(text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        return _term_vector(text)


class TermVectorIndex:
    """Vector index and dense search store over term vectors, deterministic order."""

    def __init__(self) -> None:
        self.records: dict[str, VectorRecord] = {}

    async def upsert(self, records: Sequence[VectorRecord]) -> None:
        for record in records:
            self.records[record.chunk_id] = record

    async def delete(self, blob_names: Sequence[str]) -> None:
        names = set(blob_names)
        self.records = {
            key: record
            for key, record in self.records.items()
            if record.blob_name not in names
        }

    async def search(
        self,
        *,
        query_vector: list[float],
        allowed_blob_names: Sequence[str] | None = None,
        top_k: int = 50,
        vector_threshold: float = 0.0,
    ) -> list[SearchHit]:
        allowed = set(allowed_blob_names) if allowed_blob_names is not None else None
        scored = [
            (_cosine(query_vector, record.vector), record.chunk_id, record)
            for record in self.records.values()
            if allowed is None or record.blob_name in allowed
        ]
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [
            SearchHit(
                blob_name=record.blob_name,
                path=record.path,
                content=record.content,
                score=score,
                content_hash=record.content_hash,
                start_line=record.start_line,
                end_line=record.end_line,
                context=record.context,
            )
            for score, _chunk_id, record in scored[:top_k]
            if score >= vector_threshold
        ]


class TermPathStore:
    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}

    async def insert(self, path_docs: list[dict[str, Any]]) -> dict[str, Any]:
        for doc in path_docs:
            self.docs[doc["path_id"]] = doc
        return {"inserted": len(path_docs)}

    async def delete_by_blob_names(self, blob_names: list[str]) -> None:
        names = set(blob_names)
        self.docs = {
            key: doc for key, doc in self.docs.items() if doc["blob_name"] not in names
        }

    async def search_paths(
        self,
        query_vector: list[float],
        allowed_blob_names: list[str] | None = None,
        top_k: int = 20,
    ) -> list[PathSearchResult]:
        allowed = set(allowed_blob_names) if allowed_blob_names is not None else None
        scored = [
            (_cosine(query_vector, doc["path_vector"]), doc["path"], doc)
            for doc in self.docs.values()
            if allowed is None or doc["blob_name"] in allowed
        ]
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [
            PathSearchResult(path=doc["path"], blob_name=doc["blob_name"], score=score)
            for score, _path, doc in scored[:top_k]
        ]


def _read_corpus(root: Path, max_bytes: int) -> dict[str, str]:
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        if path.stat().st_size > max_bytes:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        files[path.relative_to(root.parent).as_posix()] = text
    return files


async def _index(
    files: dict[str, str],
) -> tuple[
    async_sessionmaker[AsyncSession], TermVectorIndex, TermPathStore, dict[str, str]
]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.run_sync(create_lexical_table)
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    vector_index = TermVectorIndex()
    path_store = TermPathStore()
    provider = TreeSitterSymbolProvider(RegexSymbolProvider())
    chunker = build_chunker(
        semantic_enabled=True,
        semantic_max_chunk_chars=1_800,
        recursive_chunk_size=6_000,
        recursive_chunk_overlap=200,
    )
    names: dict[str, str] = {}
    async with SqlAlchemyUnitOfWork(sessions, provider) as uow:
        pipeline = IndexingPipeline(
            chunker=chunker,
            embedder=TermEmbedder(),
            vector_index=vector_index,
            blob_repo=uow.blobs,
            chunk_repo=uow.chunks,
            symbol_projection=uow.symbols,
            lexical_projection=uow.lexical,
            path_store=path_store,
        )
        for path, content in files.items():
            name = compute_blob_name(path, content)
            names[path] = name
            await pipeline.ingest(name, path, content)
        await pipeline.embed_pending(list(names.values()))
        await uow.commit()
    return sessions, vector_index, path_store, names


def _pipeline(
    sessions: async_sessionmaker[AsyncSession],
    vector_index: TermVectorIndex,
    path_store: TermPathStore,
    overrides: dict[str, Any],
) -> RetrievalPipeline:
    symbol_store = SymbolSearchStore(sessions)
    settings = dict(overrides)
    reranker = PathOrderReranker() if settings.pop("reranker", False) else None
    return RetrievalPipeline(
        embedder=TermEmbedder(),
        store=vector_index,
        reranker=reranker,
        path_store=path_store,
        path_content_store=SqlPathContentStore(sessions),
        path_lookup_store=SqlPathLookupStore(sessions),
        exact_store=symbol_store,
        relation_store=symbol_store,
        lexical_store=SqlLexicalSearchStore(sessions),
        settings=RetrievalSettings(confidence_floor=0.0, **settings),
    )


def _hit_record(hit: SearchHit) -> dict[str, Any]:
    return {
        "role": hit.role,
        "path": hit.path,
        "start": hit.start_line,
        "end": hit.end_line,
        "hop": hit.hop,
        "context": hit.context,
        "score": round(hit.score, 9),
        "content": hashlib.sha256(hit.content.encode("utf-8")).hexdigest()[:16],
    }


async def _dump(corpus: Path, queries: Sequence[str], max_bytes: int) -> dict[str, Any]:
    files = _read_corpus(corpus, max_bytes)
    if not files:
        raise SystemExit(f"no text files under {corpus}")
    sessions, vector_index, path_store, names = await _index(files)
    scope = SearchScope(frozenset(names.values()))
    corpus_digest = hashlib.sha256(
        "\n".join(f"{path}\n{name}" for path, name in sorted(names.items())).encode()
    ).hexdigest()
    results: list[dict[str, Any]] = []
    for profile, overrides in PROFILES.items():
        pipeline = _pipeline(sessions, vector_index, path_store, overrides)
        for query in queries:
            audit = RetrievalAudit()
            hits = await pipeline.search(query, scope, audit=audit)
            results.append(
                {
                    "profile": profile,
                    "query": query,
                    "intent": audit.intent,
                    "scope_size": audit.scope_size,
                    "path_boosted": audit.path_boosted,
                    "dense_route": audit.dense_route,
                    "rerank_route": audit.rerank_route,
                    "head_slots": audit.head_slots,
                    "exact_definitions": audit.exact_definitions,
                    "definition_sites": audit.definition_sites,
                    "relation_counts": dict(sorted(audit.relation_counts.items())),
                    "relation_chars": audit.relation_chars,
                    "stages": sorted(audit.stages),
                    "lane_failures": dict(
                        sorted(getattr(audit, "lane_failures", {}).items())
                    ),
                    "hits": [_hit_record(hit) for hit in hits],
                }
            )
    return {
        "corpus": {
            "root": corpus.as_posix(),
            "files": len(files),
            "digest": corpus_digest,
        },
        "profiles": PROFILES,
        "results": results,
    }


def _compare(before: dict[str, Any], after: dict[str, Any]) -> int:
    if before["corpus"]["digest"] != after["corpus"]["digest"]:
        print("corpus differs; the dumps are not comparable")
        return 2
    index_before = {(r["profile"], r["query"]): r for r in before["results"]}
    index_after = {(r["profile"], r["query"]): r for r in after["results"]}
    keys = sorted(set(index_before) | set(index_after))
    differing = 0
    for key in keys:
        left, right = index_before.get(key), index_after.get(key)
        if left == right:
            continue
        differing += 1
        profile, query = key
        print(f"--- {profile}: {query.splitlines()[0][:80]}")
        if left is None or right is None:
            print("    present in only one dump")
            continue
        for field in (
            "intent",
            "scope_size",
            "path_boosted",
            "dense_route",
            "rerank_route",
            "head_slots",
            "exact_definitions",
            "definition_sites",
            "relation_counts",
            "relation_chars",
            "stages",
            "lane_failures",
        ):
            if left[field] != right[field]:
                print(f"    {field}: {left[field]!r} -> {right[field]!r}")
        if left["hits"] != right["hits"]:
            print(f"    hits: {len(left['hits'])} -> {len(right['hits'])}")
            for index, (a, b) in enumerate(
                zip(left["hits"], right["hits"], strict=False)
            ):
                if a != b:
                    print(f"      #{index}: {a}")
                    print(f"          -> {b}")
                    break
    total = len(keys)
    print(f"{total - differing}/{total} (profile, query) pairs identical")
    return 1 if differing else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    dump = commands.add_parser("dump", help="index the corpus and record every query")
    dump.add_argument("--corpus", default="src/oce", type=Path)
    dump.add_argument("--queries", type=Path, default=None)
    dump.add_argument("--max-bytes", type=int, default=200_000)
    dump.add_argument("--out", type=Path, required=True)
    compare = commands.add_parser("compare", help="diff two dumps")
    compare.add_argument("before", type=Path)
    compare.add_argument("after", type=Path)
    args = parser.parse_args(argv)
    if args.command == "dump":
        queries: Sequence[str] = QUERIES
        if args.queries is not None:
            queries = json.loads(args.queries.read_text(encoding="utf-8"))
        payload = asyncio.run(_dump(args.corpus, queries, args.max_bytes))
        args.out.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True),
            encoding="utf-8",
        )
        print(
            f"{len(payload['results'])} results over {payload['corpus']['files']} files -> {args.out}"
        )
        return 0
    before = json.loads(args.before.read_text(encoding="utf-8"))
    after = json.loads(args.after.read_text(encoding="utf-8"))
    return _compare(before, after)


if __name__ == "__main__":
    sys.exit(main())
