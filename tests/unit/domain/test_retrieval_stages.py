"""Lexical fusion, path lookup, working-set prior, and result expansion."""

from __future__ import annotations

from dataclasses import replace

import pytest

from oce.domain.services.retrieval import (
    RetrievalPipeline,
    definition_excerpt,
    merge_adjacent_hits,
)
from oce.domain.services.search import DefinitionHit, SearchHit, SearchScope
from oce.shared.config.settings import RetrievalSettings
from oce.shared.metrics import RetrievalAudit
from tests.unit.domain.test_retrieval import (
    FakeEmbedder,
    FakeExactSearchStore,
    FakePathContentStore,
    FakeSearchStore,
)

BLOB_A = "a" * 64
BLOB_B = "b" * 64
BLOB_C = "c" * 64


def _hit(path, score, *, blob=BLOB_A, content="code", start=1, end=1, hash_=""):
    return SearchHit(
        blob_name=blob,
        path=path,
        content=content,
        score=score,
        content_hash=hash_,
        start_line=start,
        end_line=end,
    )


class FakeLexicalStore:
    def __init__(self, hits=None):
        self.hits = hits or []
        self.calls: list[dict] = []

    async def search_lexical(self, *, terms, phrases, scope, top_k=30):
        self.calls.append({"terms": terms, "phrases": phrases, "top_k": top_k})
        return list(self.hits)


class FakePathLookupStore:
    def __init__(self, scores=None):
        self.scores = scores or {}
        self.calls: list[dict] = []

    async def match_paths(self, *, filenames, paths, scope, limit=20):
        self.calls.append({"filenames": filenames, "paths": paths})
        return dict(self.scores)


class DefinitionStore(FakeExactSearchStore):
    def __init__(self, definitions):
        super().__init__()
        self.definitions = definitions
        self.requested: list[str] = []

    async def find_definitions(self, *, identifiers, scope, max_per_identifier=3):
        self.requested = list(identifiers)
        return [d for d in self.definitions if d.identifier in identifiers]


def _settings(**kwargs):
    return RetrievalSettings(confidence_floor=0.0, final_select_k=10, **kwargs)


class TestLexicalRecall:
    async def test_lexical_hits_are_fused_by_rank(self):
        dense = [_hit("src/a.py", 0.9, blob=BLOB_A), _hit("src/b.py", 0.8, blob=BLOB_B)]
        lexical = [
            _hit("src/c.py", 5.0, blob=BLOB_C),
            _hit("src/b.py", 4.0, blob=BLOB_B),
        ]
        store = FakeLexicalStore(lexical)
        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=FakeSearchStore(dense),
            lexical_store=store,
            settings=_settings(related_definitions_enabled=False),
        )
        audit = RetrievalAudit()
        hits = await pipe.search(
            'Why does request handling fail with "connection pool exhausted"?',
            SearchScope(frozenset({BLOB_A, BLOB_B, BLOB_C})),
            audit=audit,
        )
        # b.py ranks in both lists, so reciprocal rank fusion puts it first.
        assert [hit.path for hit in hits][0] == "src/b.py"
        assert {hit.path for hit in hits} == {"src/a.py", "src/b.py", "src/c.py"}
        assert store.calls[0]["phrases"] == ("connection pool exhausted",)
        assert "request" in store.calls[0]["terms"]
        assert "lexical" in audit.stages

    async def test_lexical_disabled_skips_store(self):
        store = FakeLexicalStore([_hit("src/c.py", 1.0)])
        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=FakeSearchStore([_hit("src/a.py", 0.9)]),
            lexical_store=store,
            settings=_settings(lexical_enabled=False),
        )
        await pipe.search("anything at all", SearchScope(frozenset({BLOB_A})))
        assert store.calls == []

    @pytest.mark.parametrize(
        ("query", "expected_calls"),
        [
            ("where is `TargetService` defined?", 1),
            ("where is src/target_service.py file?", 1),
            ("explain the repository architecture", 1),
            ("where is `TargetService` referenced?", 1),
            ("how is request retry behavior implemented?", 1),
        ],
    )
    async def test_lexical_recall_is_routed_by_query_value(self, query, expected_calls):
        store = FakeLexicalStore([_hit("src/a.py", 1.0)])
        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=FakeSearchStore([_hit("src/a.py", 0.9)]),
            lexical_store=store,
            settings=_settings(related_definitions_enabled=False),
        )

        await pipe.search(query, SearchScope(frozenset({BLOB_A})))

        assert len(store.calls) == expected_calls

    async def test_structural_symbol_and_path_hits_skip_lexical_io(self):
        lexical = FakeLexicalStore([_hit("src/noise.py", 1.0, blob=BLOB_C)])
        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=FakeSearchStore([_hit("src/semantic.py", 0.9, blob=BLOB_A)]),
            exact_store=FakeExactSearchStore(
                [_hit("src/definition.py", 0.8, blob=BLOB_B)]
            ),
            path_lookup_store=FakePathLookupStore({BLOB_B: 1.0}),
            lexical_store=lexical,
            settings=_settings(related_definitions_enabled=False),
        )
        scope = SearchScope(frozenset({BLOB_A, BLOB_B, BLOB_C}))

        await pipe.search("where is `TargetService` defined?", scope)
        await pipe.search("where is src/definition.py file?", scope)

        assert lexical.calls == []

    async def test_symbol_uses_lexical_only_after_exact_misses(self):
        lexical = FakeLexicalStore([_hit("src/fallback.py", 1.0)])
        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=FakeSearchStore(),
            exact_store=FakeExactSearchStore(),
            lexical_store=lexical,
            settings=_settings(related_definitions_enabled=False),
        )

        hits = await pipe.search(
            "where is `MissingService` defined?",
            SearchScope(frozenset({BLOB_A})),
        )

        assert [hit.path for hit in hits] == ["src/fallback.py"]
        assert len(lexical.calls) == 1
        assert lexical.calls[0]["terms"] == ("missingservice",)

    async def test_quoted_phrase_keeps_eager_lexical_recall_for_symbol(self):
        lexical = FakeLexicalStore([_hit("src/error.py", 1.0, blob=BLOB_B)])
        exact = FakeExactSearchStore([_hit("src/definition.py", 0.2)])
        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=FakeSearchStore(),
            exact_store=exact,
            lexical_store=lexical,
            settings=_settings(related_definitions_enabled=False),
        )

        hits = await pipe.search(
            'where is `TargetService` defined after "connection pool exhausted"?',
            SearchScope(frozenset({BLOB_A, BLOB_B})),
        )

        assert hits[0].path == "src/definition.py"
        assert len(lexical.calls) == 1

    async def test_lexical_failure_degrades_to_dense(self):
        class Broken(FakeLexicalStore):
            async def search_lexical(self, **kwargs):
                raise RuntimeError("fts down")

        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=FakeSearchStore([_hit("src/a.py", 0.9)]),
            lexical_store=Broken(),
            settings=_settings(),
        )
        hits = await pipe.search("find things", SearchScope(frozenset({BLOB_A})))
        assert [hit.path for hit in hits] == ["src/a.py"]

    async def test_lexical_only_result_survives_empty_dense_recall(self):
        lexical = [_hit("src/c.py", 1.0, blob=BLOB_C)]
        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=FakeSearchStore(),
            lexical_store=FakeLexicalStore(lexical),
            settings=_settings(related_definitions_enabled=False),
        )
        hits = await pipe.search(
            'error "connection pool exhausted"',
            SearchScope(frozenset({BLOB_C})),
        )
        assert hits == lexical

    async def test_embedding_failure_degrades_to_sql_lexical_recall(self):
        class BrokenEmbedder:
            async def embed_query(self, text):
                raise RuntimeError("embedding down")

        lexical = [_hit("src/c.py", 1.0, blob=BLOB_C)]
        pipe = RetrievalPipeline(
            embedder=BrokenEmbedder(),
            store=FakeSearchStore(),
            lexical_store=FakeLexicalStore(lexical),
            settings=_settings(related_definitions_enabled=False),
        )
        hits = await pipe.search(
            'error "connection pool exhausted"',
            SearchScope(frozenset({BLOB_C})),
        )
        assert hits == lexical

    async def test_embedding_failure_is_raised_when_sql_recall_finds_nothing(self):
        class BrokenEmbedder:
            async def embed_query(self, text):
                raise RuntimeError("embedding down")

        pipe = RetrievalPipeline(
            embedder=BrokenEmbedder(),
            store=FakeSearchStore(),
            lexical_store=FakeLexicalStore(),
            settings=_settings(related_definitions_enabled=False),
        )
        with pytest.raises(RuntimeError, match="embedding down"):
            await pipe.search("find connection pool", SearchScope(frozenset({BLOB_C})))


class TestPathLookup:
    async def test_traceback_paths_boost_without_backfill(self):
        dense = [
            _hit("src/a.py", 0.9, blob=BLOB_A),
            _hit("src/b.py", 0.85, blob=BLOB_B),
        ]
        lookup = FakePathLookupStore({BLOB_B: 1.0, BLOB_C: 1.0})
        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=FakeSearchStore(dense),
            path_lookup_store=lookup,
            settings=_settings(related_definitions_enabled=False),
        )
        hits = await pipe.search(
            'Traceback\n  File "pkg/src/b.py", line 3, in run\nKeyError: missing',
            SearchScope(frozenset({BLOB_A, BLOB_B, BLOB_C})),
        )
        assert lookup.calls[0]["paths"] == ("pkg/src/b.py",)
        assert [hit.path for hit in hits] == ["src/b.py", "src/a.py"]

    async def test_exact_kinds_follow_intent(self):
        exact = FakeExactSearchStore([_hit("src/x.py", 1.0)])
        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=FakeSearchStore([_hit("src/a.py", 0.9)]),
            exact_store=exact,
            settings=_settings(related_definitions_enabled=False),
        )
        scope = SearchScope(frozenset({BLOB_A}))
        await pipe.search("where is `load_config` defined", scope)
        assert exact.kinds == ("endpoint", "definition")
        await pipe.search("which modules import `load_config`", scope)
        assert exact.kinds is None

    async def test_path_lookup_backfills_when_embedding_is_unavailable(self):
        class BrokenEmbedder:
            async def embed_query(self, text):
                raise RuntimeError("embedding down")

        representative = _hit("src/b.py", 0.0, blob=BLOB_B)
        pipe = RetrievalPipeline(
            embedder=BrokenEmbedder(),
            store=FakeSearchStore(),
            path_lookup_store=FakePathLookupStore({BLOB_B: 1.0}),
            path_content_store=FakePathContentStore([representative]),
            settings=_settings(
                lexical_enabled=False,
                exact_enabled=False,
                related_definitions_enabled=False,
            ),
        )
        hits = await pipe.search("Where is src/b.py?", SearchScope(frozenset({BLOB_B})))
        assert [hit.path for hit in hits] == ["src/b.py"]

    async def test_explicit_path_lookup_owns_the_deterministic_head(self):
        lookup = FakePathLookupStore({BLOB_B: 1.0})
        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=FakeSearchStore(
                [
                    _hit("src/nearby.py", 0.99, blob=BLOB_A),
                    _hit("src/target.py", 0.10, blob=BLOB_B),
                ]
            ),
            lexical_store=FakeLexicalStore([_hit("src/nearby.py", 10.0, blob=BLOB_A)]),
            path_lookup_store=lookup,
            settings=_settings(related_definitions_enabled=False),
        )

        hits = await pipe.search(
            "Where is the src/target.py file?",
            SearchScope(frozenset({BLOB_A, BLOB_B})),
        )

        assert hits[0].path == "src/target.py"

    async def test_exact_path_lookup_skips_adaptive_reranking(self):
        class CountingReranker:
            def __init__(self):
                self.calls = 0

            async def rerank(self, query, hits):
                self.calls += 1
                return hits

        reranker = CountingReranker()
        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=FakeSearchStore(
                [
                    _hit("src/nearby.py", 0.9, blob=BLOB_A),
                    _hit("src/target.py", 0.8, blob=BLOB_B),
                ]
            ),
            path_lookup_store=FakePathLookupStore({BLOB_B: 1.0}),
            reranker=reranker,
            settings=_settings(related_definitions_enabled=False),
        )
        audit = RetrievalAudit()

        await pipe.search(
            "Where is the src/target.py file?",
            SearchScope(frozenset({BLOB_A, BLOB_B})),
            audit=audit,
        )

        assert reranker.calls == 0
        assert audit.rerank_route == "skip:path_evidence"


class TestWorkingSetPrior:
    async def test_added_blobs_are_boosted(self):
        dense = [
            _hit("src/a.py", 0.80, blob=BLOB_A),
            _hit("src/b.py", 0.75, blob=BLOB_B),
        ]
        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=FakeSearchStore(dense),
            settings=_settings(working_set_boost=1.2),
        )
        scope = SearchScope(
            frozenset({BLOB_A, BLOB_B}), added_blob_names=frozenset({BLOB_B})
        )
        hits = await pipe.search("recent work", scope)
        assert [hit.path for hit in hits] == ["src/b.py", "src/a.py"]

    async def test_large_delta_is_not_boosted(self):
        dense = [
            _hit("src/a.py", 0.80, blob=BLOB_A),
            _hit("src/b.py", 0.75, blob=BLOB_B),
        ]
        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=FakeSearchStore(dense),
            settings=_settings(working_set_boost=1.2, working_set_boost_max_blobs=1),
        )
        scope = SearchScope(
            frozenset({BLOB_A, BLOB_B}), added_blob_names=frozenset({BLOB_A, BLOB_B})
        )
        hits = await pipe.search("recent work", scope)
        assert [hit.path for hit in hits] == ["src/a.py", "src/b.py"]


class TestMergeAdjacent:
    def test_touching_and_overlapping_spans_merge_in_rank_position(self):
        hits = [
            _hit("src/a.py", 0.9, content="l5\nl6", start=5, end=6),
            _hit("src/b.py", 0.8, blob=BLOB_B, content="x", start=1, end=1),
            _hit("src/a.py", 0.7, content="l7\nl8", start=7, end=8),
            _hit("src/a.py", 0.6, content="l8\nl9", start=8, end=9),
            _hit("src/a.py", 0.5, content="l20", start=20, end=20),
        ]
        merged = merge_adjacent_hits(hits)
        assert [(h.path, h.start_line, h.end_line) for h in merged] == [
            ("src/a.py", 5, 9),
            ("src/b.py", 1, 1),
            ("src/a.py", 20, 20),
        ]
        assert merged[0].content == "l5\nl6\nl7\nl8\nl9"
        assert merged[0].score == 0.9

    def test_same_blob_different_paths_do_not_merge(self):
        hits = [_hit("a.py", 1.0, start=1, end=1), _hit("b.py", 0.9, start=2, end=2)]
        assert len(merge_adjacent_hits(hits)) == 2

    def test_nested_overlap_uses_the_cluster_maximum_end(self):
        hits = [
            _hit(
                "a.py",
                1.0,
                content="\n".join(f"l{i}" for i in range(1, 11)),
                start=1,
                end=10,
            ),
            _hit("a.py", 0.9, content="l2\nl3", start=2, end=3),
            _hit("a.py", 0.8, content="l9\nl10\nl11\nl12", start=9, end=12),
        ]
        merged = merge_adjacent_hits(hits)
        assert [(hit.start_line, hit.end_line) for hit in merged] == [(1, 12)]

    def test_different_scope_contexts_are_not_mislabeled(self):
        hits = [
            replace(_hit("a.py", 1.0, content="a", start=1, end=1), context="class A"),
            replace(_hit("a.py", 0.9, content="b", start=2, end=2), context="class B"),
        ]
        merged = merge_adjacent_hits(hits)
        assert len(merged) == 1
        assert merged[0].context is None


class TestRelatedDefinitions:
    def _definition(self, identifier, path, blob, chunk_start, def_start, body):
        chunk = SearchHit(
            blob_name=blob,
            path=path,
            content=body,
            score=0.9,
            content_hash="f" * 64,
            start_line=chunk_start,
            end_line=chunk_start + len(body.splitlines()) - 1,
            context="module",
        )
        return DefinitionHit(identifier, "definition", chunk, def_start, def_start + 2)

    def test_excerpt_cuts_definition_lines_from_chunk(self):
        definition = self._definition(
            "Base",
            "src/base.py",
            BLOB_B,
            10,
            12,
            "x = 1\n\nclass Base:\n    a = 1\n    b = 2\n\nz = 3",
        )
        excerpt = definition_excerpt(definition, max_lines=2)
        assert excerpt is not None
        assert (excerpt.start_line, excerpt.end_line) == (12, 13)
        assert excerpt.content == "class Base:\n    a = 1"
        assert excerpt.role == "related"
        assert excerpt.context == "module"

    async def test_query_and_mined_identifiers_pull_definitions(self):
        primary = _hit(
            "src/service.py",
            0.9,
            blob=BLOB_A,
            content="class Service(BaseService):\n    def run(self):\n        return helper_fn()",
            start=1,
            end=3,
            hash_="1" * 64,
        )
        definitions = [
            self._definition(
                "BaseService",
                "src/base.py",
                BLOB_B,
                1,
                1,
                "class BaseService:\n    pass",
            ),
            self._definition(
                "helper_fn",
                "src/util.py",
                BLOB_C,
                5,
                5,
                "def helper_fn():\n    return 1",
            ),
            # Defined inside the selected chunk itself: must be skipped.
            DefinitionHit("Service", "definition", primary, 1, 3),
        ]
        store = DefinitionStore(definitions)
        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=FakeSearchStore([primary]),
            exact_store=store,
            settings=_settings(related_snippet_lines=1),
        )
        hits = await pipe.search(
            "how does `Service` call `helper_fn`?",
            SearchScope(frozenset({BLOB_A, BLOB_B, BLOB_C})),
        )
        roles = [(hit.role, hit.path) for hit in hits]
        assert roles[0] == ("primary", "src/service.py")
        assert ("related", "src/base.py") in roles
        assert ("related", "src/util.py") in roles
        assert not any(
            hit.role == "related" and hit.path == "src/service.py" for hit in hits
        )
        # The query identifier is asked for first.
        assert store.requested[0] == "Service"
        related = [hit for hit in hits if hit.role == "related"]
        assert all(hit.end_line == hit.start_line for hit in related)

    async def test_chunk_context_contributes_structural_identifiers(self):
        primary = replace(
            _hit(
                "src/service.py",
                0.9,
                content="def run(self):\n    return True",
                hash_="1" * 64,
            ),
            context="class Service(BaseService):",
        )
        store = DefinitionStore(
            [
                self._definition(
                    "BaseService",
                    "src/base.py",
                    BLOB_B,
                    1,
                    1,
                    "class BaseService:\n    pass",
                )
            ]
        )
        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=FakeSearchStore([primary]),
            exact_store=store,
            settings=_settings(),
        )

        hits = await pipe.search(
            "how is `run` called?", SearchScope(frozenset({BLOB_A, BLOB_B}))
        )

        assert "BaseService" in store.requested
        assert any(hit.role == "related" and hit.path == "src/base.py" for hit in hits)

    async def test_related_definitions_expand_semantic_relationship_queries(self):
        primary = _hit(
            "src/service.py",
            0.9,
            content="def run():\n    return helper_fn()",
            hash_="1" * 64,
        )
        store = DefinitionStore(
            [
                self._definition(
                    "helper_fn",
                    "src/util.py",
                    BLOB_B,
                    1,
                    1,
                    "def helper_fn():\n    return 1",
                )
            ]
        )
        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=FakeSearchStore([primary]),
            exact_store=store,
            settings=_settings(),
        )

        feature = await pipe.search(
            "explain the retry behavior", SearchScope(frozenset({BLOB_A, BLOB_B}))
        )
        assert any(hit.role == "related" for hit in feature)

        symbol = await pipe.search(
            "where is `run` defined?", SearchScope(frozenset({BLOB_A, BLOB_B}))
        )
        assert all(hit.role == "primary" for hit in symbol)

        call_chain = await pipe.search(
            "how does `run` call `helper_fn`?",
            SearchScope(frozenset({BLOB_A, BLOB_B})),
        )
        assert any(hit.role == "related" for hit in call_chain)

    async def test_related_definitions_share_the_primary_context_budget(self):
        primary = _hit(
            "src/service.py",
            0.9,
            content="helper_fn()",
            hash_="1" * 64,
        )
        store = DefinitionStore(
            [
                self._definition(
                    "helper_fn",
                    "src/util.py",
                    BLOB_B,
                    1,
                    1,
                    "def helper_fn():\n    return 1",
                )
            ]
        )
        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=FakeSearchStore([primary]),
            exact_store=store,
            settings=_settings(max_context_chars=len(primary.content)),
        )

        hits = await pipe.search(
            "how does `run` call `helper_fn`?",
            SearchScope(frozenset({BLOB_A, BLOB_B})),
        )

        assert all(hit.role == "primary" for hit in hits)

    async def test_related_budget_and_switch(self):
        primary = _hit("src/s.py", 0.9, content="use_thing()", hash_="1" * 64)
        store = DefinitionStore(
            [
                self._definition(
                    "use_thing",
                    "src/t.py",
                    BLOB_B,
                    1,
                    1,
                    "def use_thing():\n    return 1",
                )
            ]
        )
        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=FakeSearchStore([primary]),
            exact_store=store,
            settings=_settings(related_definitions_enabled=False),
        )
        hits = await pipe.search("thing", SearchScope(frozenset({BLOB_A, BLOB_B})))
        assert all(hit.role == "primary" for hit in hits)

        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=FakeSearchStore([primary]),
            exact_store=store,
            settings=_settings(related_max_chars=5),
        )
        hits = await pipe.search("thing", SearchScope(frozenset({BLOB_A, BLOB_B})))
        assert all(hit.role == "primary" for hit in hits)


@pytest.mark.parametrize("query", ["find core"])
async def test_empty_scope_short_circuits(query):
    pipe = RetrievalPipeline(
        embedder=FakeEmbedder(),
        store=FakeSearchStore([_hit("a", 1.0)]),
        settings=_settings(),
    )
    assert await pipe.search(query, SearchScope(frozenset())) == []
