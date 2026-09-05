"""RetrievalPipeline 领域服务测试

用 Fake store / embedder 验证编排流程：
embed → search → 源码优先/召回过滤 → rerank → select。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from oce.domain.services.path_search import PathSearchResult
from oce.domain.services.query_classifier import classify_query_intent
from oce.domain.services.retrieval import RetrievalPipeline, source_priority_factor
from oce.domain.services.search import SearchHit, SearchScope
from oce.shared.config.settings import RetrievalSettings
from oce.shared.metrics import RetrievalAudit

# The store only sees vectors; the fake embedder registers each query text under
# its vector so the store can still answer per query.
_TEXT_BY_VECTOR: dict[tuple[float, ...], str] = {}


class FakeSearchStore:
    """SearchStore 内存替身：返回预设命中"""

    def __init__(self, hits=None):
        self.hits = hits or []
        self.last_query: str = ""
        self.last_vector: list[float] = []
        self.queries: list[str] = []
        self.hits_by_query: dict[str, list[SearchHit]] = {}

    async def search(
        self,
        *,
        query_vector,
        allowed_blob_names=None,
        top_k=50,
        vector_threshold=0.0,
    ):
        query = _TEXT_BY_VECTOR.get(tuple(query_vector), "")
        self.last_query = query
        self.last_vector = query_vector
        self.queries.append(query)
        return list(self.hits_by_query.get(query, self.hits))


class FakeEmbedder:
    """确定性假 embedder"""

    def __init__(self):
        self.queries: list[str] = []

    async def embed_query(self, text):
        self.queries.append(text)
        vector = [float(len(text)), float(sum(map(ord, text)) % 9973)]
        _TEXT_BY_VECTOR[tuple(vector)] = text
        return vector


class FakePathStore:
    def __init__(self, results=None):
        self.queries = 0
        self.results = results or []

    async def search_paths(self, query_vector, allowed_blob_names=None, top_k=20):
        self.queries += 1
        return list(self.results)


class FakePathContentStore:
    def __init__(self, hits=None, error: Exception | None = None):
        self.hits = hits or []
        self.error = error
        self.blob_names: tuple[str, ...] = ()

    async def get_representative_chunks(self, blob_names):
        self.blob_names = tuple(blob_names)
        if self.error is not None:
            raise self.error
        return list(self.hits)


class FakeExactSearchStore:
    def __init__(self, hits=None, error: Exception | None = None):
        self.hits = hits or []
        self.error = error
        self.identifiers: tuple[str, ...] = ()
        self.scope: SearchScope | None = None
        self.kinds_seen: list[tuple[str, ...] | None] = []

    async def search_exact(self, *, identifiers, scope, top_k=50, kinds=None):
        self.identifiers = tuple(identifiers)
        self.scope = scope
        self.kinds = kinds
        self.kinds_seen.append(kinds)
        if self.error is not None:
            raise self.error
        return list(self.hits[:top_k])

    async def find_definitions(self, *, identifiers, scope, max_per_identifier=3):
        return []


def _hit(path: str, score: float) -> SearchHit:
    return SearchHit(blob_name="x" * 64, path=path, content="code", score=score)


def _scope(*blob_names: str) -> SearchScope:
    return SearchScope(frozenset(blob_names))


def _settings(**kwargs) -> RetrievalSettings:
    return RetrievalSettings(**kwargs)


class TestSourcePriorityFactor:
    def test_source_code_is_one(self):
        assert source_priority_factor("src/engine/core.py") == 1.0

    def test_main_readme_is_one(self):
        assert source_priority_factor("README.md") == 1.0

    def test_legal_file_heavily_penalized(self):
        assert source_priority_factor("LICENSE") < 0.5

    def test_docs_and_tests_penalized(self):
        assert source_priority_factor("docs/guide.md") < 1.0
        assert source_priority_factor("tests/test_x.py") < 1.0
        assert source_priority_factor("src/tools/planner.test.ts") == 0.6

    def test_generic_barrels_and_types_are_slightly_penalized(self):
        assert source_priority_factor("src/tools/index.ts") == 0.85
        assert source_priority_factor("src/tools/types.ts") == 0.85
        assert source_priority_factor("src/config/types.openclaw.ts") == 1.0
        assert source_priority_factor("requests/__init__.py") == 0.85

    def test_changelogs_examples_and_singular_doc_dir_are_documentation(self):
        assert source_priority_factor("ChangeLog") == 0.5
        assert source_priority_factor("changelog/README.rst") == 0.5
        assert source_priority_factor("doc/data/messages/c/bad.py") == 0.5
        assert source_priority_factor("examples/pyproject.toml") == 0.5
        assert source_priority_factor("README.md") == 1.0
        assert source_priority_factor("README.zh-CN.md") == 0.2
        # A source module that happens to use a documentation-like stem is
        # implementation code; only root metadata files get the stem rule.
        assert source_priority_factor("src/history.py") == 1.0
        assert source_priority_factor("history.py") == 1.0

    def test_typescript_and_go_test_conventions_are_tests(self):
        assert source_priority_factor("packages/toolkit/src/tests/a.test.ts") == 0.6
        assert source_priority_factor("src/configureStore.test-d.ts") == 0.6
        assert source_priority_factor("src/__tests__/store.ts") == 0.6
        assert (
            source_priority_factor("codemods/x/__testfixtures__/basic.input.js") == 0.6
        )
        assert source_priority_factor("pkg/router_test.go") == 0.6
        assert source_priority_factor("src/_pytest/fixtures.py") == 1.0

    def test_unparsed_languages_yield_to_project_code(self):
        assert source_priority_factor("elisp/pylint.el") == 0.85
        assert source_priority_factor("tools/build.ps1") == 0.85
        assert source_priority_factor("pylint/checkers/variables.py") == 1.0
        assert source_priority_factor("Makefile") == 1.0

    def test_benchmark_harnesses_are_supporting_material(self):
        assert source_priority_factor("asv_bench/benchmarks/combine.py") == 0.6
        assert source_priority_factor("benches/router.rs") == 0.6
        assert source_priority_factor("src/perf/counter.py") == 0.6

    def test_config_files_and_stubs_yield_to_implementation(self):
        assert source_priority_factor(".coveragerc") == 0.7
        assert source_priority_factor("pylintrc") == 0.7
        assert source_priority_factor("pyproject.toml") == 0.7
        assert source_priority_factor("setup.cfg") == 0.7
        assert source_priority_factor("xarray/core/_typed_ops.pyi") == 0.7
        assert source_priority_factor("src/engine/core.py") == 1.0


class TestRetrievalPipeline:
    @pytest.fixture
    def pipe(self):
        hits = [
            _hit("src/core.py", 0.9),
            _hit("docs/guide.md", 0.8),
            _hit("src/util.py", 0.7),
        ]
        return RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=FakeSearchStore(hits),
            settings=_settings(confidence_floor=0.0, final_select_k=10),
        )

    async def test_search_returns_sorted_results(self, pipe):
        results = await pipe.search("find core")

        # 源码优先：docs/guide.md 的 0.8 被降权到 0.4，排到 0.7 之后
        assert [r.path for r in results] == [
            "src/core.py",
            "src/util.py",
            "docs/guide.md",
        ]

    async def test_main_readme_not_penalized(self):
        hits = [
            _hit("src/core.py", 0.8),
            _hit("README.md", 0.75),  # 主 README 显式不降权
        ]
        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=FakeSearchStore(hits),
            settings=_settings(confidence_floor=0.0, final_select_k=10),
        )
        results = await pipe.search("q")
        assert [r.path for r in results] == ["src/core.py", "README.md"]

    async def test_source_priority_can_be_disabled_for_ablation(self):
        hits = [
            _hit("docs/guide.md", 0.8),
            _hit("src/core.py", 0.7),
        ]
        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=FakeSearchStore(hits),
            settings=_settings(
                source_priority_enabled=False,
                confidence_floor=0.0,
                final_select_k=10,
            ),
        )

        results = await pipe.search("q")

        assert [result.path for result in results] == [
            "docs/guide.md",
            "src/core.py",
        ]

    async def test_confidence_floor_filters_weak_hits(self):
        hits = [
            _hit("src/a.py", 0.9),
            _hit("src/b.py", 0.1),  # 低于 floor
        ]
        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=FakeSearchStore(hits),
            settings=_settings(confidence_floor=0.3, final_select_k=10),
        )
        results = await pipe.search("q")
        assert [r.path for r in results] == ["src/a.py"]

    async def test_confidence_floor_does_not_compare_model_scores(self):
        class RescoringReranker:
            async def rerank(self, query, hits):
                return [replace(hit, score=0.1) for hit in hits]

        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=FakeSearchStore([_hit("src/a.py", 0.9), _hit("src/b.py", 0.8)]),
            reranker=RescoringReranker(),
            settings=_settings(confidence_floor=0.5, final_select_k=10),
        )

        results = await pipe.search("q")

        assert [result.path for result in results] == ["src/a.py", "src/b.py"]
        assert {result.score for result in results} == {0.1}

    async def test_final_select_k_limits_results(self):
        hits = [_hit(f"src/f{i}.py", 1.0 - i * 0.01) for i in range(10)]
        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=FakeSearchStore(hits),
            settings=_settings(confidence_floor=0.0, final_select_k=3),
        )
        results = await pipe.search("q")
        assert len(results) == 3

    async def test_coverage_selection_can_be_disabled_for_topk_ablation(self):
        hits = [
            replace(_hit("src/a.py", 0.9), content_hash="a1", start_line=1),
            replace(_hit("src/a.py", 0.8), content_hash="a2", start_line=20),
            replace(_hit("src/b.py", 0.7), content_hash="b1", start_line=1),
        ]
        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=FakeSearchStore(hits),
            settings=_settings(
                coverage_selection_enabled=False,
                max_chunks_per_path=1,
                final_select_k=2,
            ),
        )

        results = await pipe.search("q")

        assert [result.path for result in results] == ["src/a.py", "src/a.py"]

    async def test_allowed_blob_names_passed_to_store(self):
        store = FakeSearchStore([_hit("src/a.py", 0.9)])
        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=store,
            settings=_settings(confidence_floor=0.0, final_select_k=10),
        )
        await pipe.search("q", _scope("aaa", "bbb"))
        assert set(store.last_query == "q" and store.last_vector)  # 触发赋值
        assert store.last_query == "q"

    async def test_empty_scope_does_not_search_globally(self):
        store = FakeSearchStore([_hit("src/private.py", 0.9)])
        embedder = FakeEmbedder()
        pipe = RetrievalPipeline(
            embedder=embedder,
            store=store,
            settings=_settings(confidence_floor=0.0, final_select_k=10),
        )

        results = await pipe.search("q", _scope())

        assert results == []
        assert embedder.queries == []
        assert store.last_query == ""

    async def test_reranker_used_when_provided(self):
        class ReorderReranker:
            async def rerank(self, query, hits):
                return list(reversed(hits))

        hits = [_hit("src/a.py", 0.5), _hit("src/b.py", 0.9)]
        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=FakeSearchStore(hits),
            reranker=ReorderReranker(),
            settings=_settings(confidence_floor=0.0, final_select_k=10),
        )
        results = await pipe.search("q")
        # source prior 在模型之前应用；reranker 返回顺序是最终相关性顺序。
        assert results[0].path == "src/a.py"

    async def test_llm_rerank_order_is_not_overwritten_by_retrieval_scores(self):
        class ReverseLLMReranker:
            def __init__(self):
                self.calls = 0

            async def rerank(self, query, candidates):
                self.calls += 1
                return list(reversed(candidates))

        llm_reranker = ReverseLLMReranker()
        hits = [_hit("src/high.py", 0.9), _hit("docs/answer.md", 0.8)]
        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=FakeSearchStore(hits),
            llm_reranker=llm_reranker,
            settings=_settings(confidence_floor=0.0, final_select_k=10),
        )

        results = await pipe.search("Explain the system architecture")

        assert llm_reranker.calls == 1
        assert [result.path for result in results] == [
            "docs/answer.md",
            "src/high.py",
        ]

    async def test_dedicated_and_llm_rerankers_form_an_ordered_cascade(self):
        stages: list[tuple[str, list[str]]] = []

        class DedicatedReranker:
            async def rerank(self, query, hits):
                stages.append(("dedicated", [hit.path for hit in hits]))
                return list(reversed(hits))

        class SemanticReranker:
            async def rerank(self, query, hits):
                stages.append(("llm", [hit.path for hit in hits]))
                return list(reversed(hits))

        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=FakeSearchStore(
                [_hit("src/lower.py", 0.5), _hit("src/higher.py", 0.9)]
            ),
            reranker=DedicatedReranker(),
            llm_reranker=SemanticReranker(),
            settings=_settings(confidence_floor=0.0, final_select_k=10),
        )

        results = await pipe.search("Explain the subsystem architecture")

        assert stages == [
            ("dedicated", ["src/higher.py", "src/lower.py"]),
            ("llm", ["src/lower.py", "src/higher.py"]),
        ]
        assert [hit.path for hit in results] == ["src/higher.py", "src/lower.py"]

    async def test_llm_rerank_skipped_for_reference_coverage(self):
        class RecordingLLMReranker:
            def __init__(self):
                self.calls = 0

            async def rerank(self, query, candidates):
                self.calls += 1
                return candidates

        llm_reranker = RecordingLLMReranker()
        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=FakeSearchStore(
                [_hit("src/winner.py", 0.95), _hit("src/other.py", 0.60)]
            ),
            llm_reranker=llm_reranker,
            settings=_settings(confidence_floor=0.0, final_select_k=10),
        )

        results = await pipe.search("Where is `winner` referenced?")

        assert llm_reranker.calls == 0
        assert [result.path for result in results] == [
            "src/winner.py",
            "src/other.py",
        ]

    async def test_symbol_intent_does_not_use_path_index(self):
        path_store = FakePathStore()
        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=FakeSearchStore([_hit("src/commands/provider.rs", 0.9)]),
            path_store=path_store,
            settings=_settings(confidence_floor=0.0, final_select_k=10),
        )

        results = await pipe.search("`add_provider` 函数在哪里定义？")

        assert path_store.queries == 0
        assert [result.path for result in results] == ["src/commands/provider.rs"]

    async def test_path_only_hit_uses_injected_content_store(self):
        blob_name = "p" * 64
        path_store = FakePathStore([PathSearchResult("src/config.py", blob_name, 0.91)])
        embedder = FakeEmbedder()
        content_store = FakePathContentStore(
            [
                SearchHit(
                    blob_name=blob_name,
                    path="src/config.py",
                    content="SETTING = True",
                    content_hash="c" * 64,
                    start_line=5,
                    end_line=5,
                    score=0.0,
                )
            ]
        )
        pipe = RetrievalPipeline(
            embedder=embedder,
            store=FakeSearchStore(),
            path_store=path_store,
            path_content_store=content_store,
            settings=_settings(confidence_floor=0.0, final_select_k=10),
        )

        results = await pipe.search("Where is config.py?")

        assert path_store.queries == 1
        assert content_store.blob_names == (blob_name,)
        assert [(hit.path, hit.content, hit.score) for hit in results] == [
            ("src/config.py", "SETTING = True", 0.91)
        ]
        assert embedder.queries == ["Where is config.py?"]

    async def test_path_content_failure_degrades_to_other_content_hits(self):
        missing_blob = "p" * 64
        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=FakeSearchStore([_hit("src/fallback.py", 0.7)]),
            path_store=FakePathStore(
                [PathSearchResult("src/missing.py", missing_blob, 0.9)]
            ),
            path_content_store=FakePathContentStore(
                error=RuntimeError("database unavailable")
            ),
            settings=_settings(confidence_floor=0.0, final_select_k=10),
        )

        results = await pipe.search("Where is missing.py?")

        assert [hit.path for hit in results] == ["src/fallback.py"]

    async def test_path_and_content_recall_overlap(self):
        path_started = asyncio.Event()
        content_started = asyncio.Event()

        class CoordinatedPathStore(FakePathStore):
            async def search_paths(self, *args, **kwargs):
                path_started.set()
                await asyncio.wait_for(content_started.wait(), timeout=0.5)
                return await super().search_paths(*args, **kwargs)

        class CoordinatedContentStore(FakeSearchStore):
            async def search(self, **kwargs):
                content_started.set()
                await asyncio.wait_for(path_started.wait(), timeout=0.5)
                return await super().search(**kwargs)

        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=CoordinatedContentStore([_hit("src/config.py", 0.8)]),
            path_store=CoordinatedPathStore(
                [PathSearchResult("src/config.py", "x" * 64, 0.9)]
            ),
            settings=_settings(confidence_floor=0.0, final_select_k=10),
        )

        results = await pipe.search("Where is config.py?")

        assert path_started.is_set()
        assert content_started.is_set()
        assert [hit.path for hit in results] == ["src/config.py"]

    async def test_path_query_propagates_content_failure_without_path_fallback(self):
        class FailingSearchStore(FakeSearchStore):
            async def search(self, **kwargs):
                raise RuntimeError("dense unavailable")

        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=FailingSearchStore(),
            path_store=FakePathStore(),
            settings=_settings(confidence_floor=0.0, final_select_k=10),
        )

        with pytest.raises(RuntimeError, match="dense unavailable"):
            await pipe.search("Where is missing.py?")

    async def test_path_query_propagates_backfill_failure_without_content_hits(self):
        blob_name = "p" * 64
        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=FakeSearchStore(),
            path_store=FakePathStore(
                [PathSearchResult("src/missing.py", blob_name, 0.9)]
            ),
            path_content_store=FakePathContentStore(
                error=RuntimeError("metadata unavailable")
            ),
            settings=_settings(confidence_floor=0.0, final_select_k=10),
        )

        with pytest.raises(RuntimeError, match="metadata unavailable"):
            await pipe.search("Where is missing.py?")

    async def test_exact_identifier_candidates_join_semantic_reranking(self):
        exact_store = FakeExactSearchStore(
            [
                SearchHit(
                    blob_name="a" * 64,
                    path="src-tauri/src/commands/copilot.rs",
                    content="pub async fn copilot_get_models() {}",
                    score=1.0,
                )
            ]
        )
        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=FakeSearchStore([_hit("src/proxy/copilot_auth.rs", 0.9)]),
            exact_store=exact_store,
            settings=_settings(confidence_floor=0.0, final_select_k=10),
        )

        results = await pipe.search(
            "`copilot_get_models` 的实现文件是？", _scope("a" * 64)
        )

        assert exact_store.identifiers == ("copilot_get_models",)
        assert exact_store.scope == _scope("a" * 64)
        assert results[0].path == "src-tauri/src/commands/copilot.rs"

    async def test_exact_symbol_heads_are_not_displaced_by_rrf_score_scale(self):
        exact_doc = SearchHit(
            blob_name="a" * 64,
            path="docs/definition.rst",
            content="class TargetService: pass",
            score=0.20,
        )
        exact = SearchHit(
            blob_name="c" * 64,
            path="src/definition.py",
            content="class TargetService: pass",
            score=0.20,
        )
        second_exact = SearchHit(
            blob_name="d" * 64,
            path="src/alternate.py",
            content="class TargetService: pass",
            score=0.19,
        )
        semantic = SearchHit(
            blob_name="b" * 64,
            path="README.rst",
            content="TargetService documentation",
            score=1.0,
        )
        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=FakeSearchStore([semantic]),
            exact_store=FakeExactSearchStore([exact_doc, exact, second_exact]),
            settings=_settings(confidence_floor=0.5, final_select_k=10),
        )

        results = await pipe.search(
            "Where is `TargetService` defined?",
            _scope(
                exact_doc.blob_name,
                exact.blob_name,
                second_exact.blob_name,
                semantic.blob_name,
            ),
        )

        assert [result.path for result in results[:3]] == [
            "src/definition.py",
            "src/alternate.py",
            "docs/definition.rst",
        ]

    async def test_exact_and_dense_recall_overlap(self):
        dense_started = asyncio.Event()
        exact_started = asyncio.Event()

        class CoordinatedSearchStore(FakeSearchStore):
            async def search(self, **kwargs):
                dense_started.set()
                await asyncio.wait_for(exact_started.wait(), timeout=0.5)
                return await super().search(**kwargs)

        class CoordinatedExactStore(FakeExactSearchStore):
            async def search_exact(self, **kwargs):
                exact_started.set()
                await asyncio.wait_for(dense_started.wait(), timeout=0.5)
                return await super().search_exact(**kwargs)

        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=CoordinatedSearchStore([_hit("src/semantic.py", 0.9)]),
            exact_store=CoordinatedExactStore([_hit("src/exact.py", 1.0)]),
            settings=_settings(confidence_floor=0.0, final_select_k=10),
        )

        results = await pipe.search("`target_symbol` 在哪里？", _scope("a" * 64))

        assert dense_started.is_set()
        assert exact_started.is_set()
        assert [hit.path for hit in results] == ["src/exact.py", "src/semantic.py"]

    async def test_path_branch_keeps_exact_identifier_recall(self):
        exact = _hit("src/exact.py", 1.0)
        exact_store = FakeExactSearchStore([exact])
        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=FakeSearchStore([_hit("src/semantic.py", 0.9)]),
            path_store=FakePathStore(),
            exact_store=exact_store,
            settings=_settings(confidence_floor=0.0, final_select_k=10),
        )

        results = await pipe.search(
            "OCE_WORKSPACE and OCE_API_URL config files",
            _scope("a" * 64),
        )

        assert exact_store.identifiers == ("OCE_WORKSPACE", "OCE_API_URL")
        assert [hit.path for hit in results] == ["src/exact.py", "src/semantic.py"]

    async def test_exact_identifier_failure_falls_back_to_semantic_results(self):
        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=FakeSearchStore([_hit("src/fallback.py", 0.9)]),
            exact_store=FakeExactSearchStore(
                error=RuntimeError("database unavailable")
            ),
            settings=_settings(confidence_floor=0.0, final_select_k=10),
        )

        results = await pipe.search("`target_symbol` 在哪里？", _scope("a" * 64))

        assert [result.path for result in results] == ["src/fallback.py"]

    async def test_exact_identifier_recall_can_be_disabled_for_ablation(self):
        exact_store = FakeExactSearchStore([_hit("src/exact.py", 1.0)])
        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=FakeSearchStore([_hit("src/semantic.py", 0.9)]),
            exact_store=exact_store,
            settings=_settings(
                exact_enabled=False,
                confidence_floor=0.0,
                final_select_k=10,
            ),
        )

        results = await pipe.search("`target_symbol` 在哪里？", _scope("a" * 64))

        assert [result.path for result in results] == ["src/semantic.py"]
        assert exact_store.identifiers == ()

    async def test_exact_identifier_requires_scope_but_accepts_large_scopes(self):
        exact_store = FakeExactSearchStore([_hit("src/exact.py", 1.0)])
        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=FakeSearchStore([_hit("src/semantic.py", 0.9)]),
            exact_store=exact_store,
            settings=_settings(confidence_floor=0.0, final_select_k=10),
        )

        unbounded = await pipe.search("`target_symbol` 在哪里？")
        large_scope = await pipe.search(
            "`target_symbol` 在哪里？",
            SearchScope(frozenset(str(index) for index in range(2_001))),
        )

        assert [hit.path for hit in unbounded] == ["src/semantic.py"]
        assert [hit.path for hit in large_scope] == [
            "src/exact.py",
            "src/semantic.py",
        ]
        assert exact_store.identifiers == ("target_symbol",)

    async def test_qualified_exact_lookup_does_not_drop_other_compound_names(self):
        session_get = SearchHit(
            blob_name="a" * 64,
            path="src/session.py",
            content="def get(self):\n    pass",
            score=1.0,
            context="class Session",
        )
        other_get = SearchHit(
            blob_name="b" * 64,
            path="src/other.py",
            content="def get(self):\n    pass",
            score=1.0,
            context="class Other",
        )
        cache = SearchHit(
            blob_name="c" * 64,
            path="src/cache.py",
            content="class Cache:\n    pass",
            score=1.0,
        )

        class ByIdentifierExactStore(FakeExactSearchStore):
            def __init__(self):
                super().__init__()
                self.calls: list[tuple[str, ...]] = []

            async def search_exact(self, *, identifiers, scope, top_k=50, kinds=None):
                self.calls.append(tuple(identifiers))
                self.scope = scope
                self.kinds_seen.append(kinds)
                return {
                    "Session.get": [],
                    "get": [session_get, other_get],
                    "Cache": [cache],
                }.get(identifiers[0], [])[:top_k]

        exact_store = ByIdentifierExactStore()
        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=FakeSearchStore(),
            exact_store=exact_store,
            settings=_settings(
                related_definitions_enabled=False,
                final_select_k=10,
            ),
        )

        results = await pipe.search(
            "Where are `Session.get` and `Cache` defined?",
            _scope(session_get.blob_name, other_get.blob_name, cache.blob_name),
        )

        assert {result.path for result in results} == {
            "src/session.py",
            "src/cache.py",
        }
        assert "src/other.py" not in {result.path for result in results}
        assert {call[0] for call in exact_store.calls} == {
            "Session.get",
            "get",
            "Cache",
        }

    def test_call_chain_exact_candidates_fill_window_without_overwriting_scores(self):
        rerank_window = 30
        semantic = [
            SearchHit(
                blob_name=str(index).zfill(64),
                path=f"src/semantic_{index}.py",
                content=f"reference {index}",
                score=1.0 - index * 0.01,
            )
            for index in range(35)
        ]
        duplicate = replace(semantic[0], score=1.1)
        exact_only = SearchHit(
            blob_name="e" * 64,
            path="src/commands/target.py",
            content="def target_symbol(): pass",
            score=1.0,
        )
        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=FakeSearchStore(),
            exact_store=FakeExactSearchStore(),
            rerank_window=rerank_window,
            settings=_settings(confidence_floor=0.0, final_select_k=10),
        )

        merged = pipe._merge_exact_hits(
            classify_query_intent("`target_symbol` 的完整调用链？"),
            [duplicate, exact_only],
            semantic,
        )

        assert merged[0].score == semantic[0].score
        assert merged[0].score != duplicate.score
        exact_position = next(
            index for index, hit in enumerate(merged) if hit.path == exact_only.path
        )
        assert exact_position < rerank_window

    async def test_confident_exact_symbol_skips_llm_and_promotes_endpoint(self):
        class HelperFirstLLMReranker:
            def __init__(self):
                self.calls = 0

            async def rerank(self, query, candidates):
                self.calls += 1
                return sorted(
                    candidates,
                    key=lambda item: "services" in item.path,
                    reverse=True,
                )

        endpoint = SearchHit(
            blob_name="a" * 64,
            path="src-tauri/src/commands/profile.rs",
            content="#[tauri::command]\npub fn delete_profile() {}",
            score=1.0,
        )
        helper = SearchHit(
            blob_name="b" * 64,
            path="src-tauri/src/services/profile.rs",
            content="pub fn delete_profile() {}",
            score=0.95,
        )
        llm_reranker = HelperFirstLLMReranker()
        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=FakeSearchStore(),
            exact_store=FakeExactSearchStore([endpoint, helper]),
            llm_reranker=llm_reranker,
            settings=_settings(confidence_floor=0.0, final_select_k=10),
        )

        results = await pipe.search(
            "`delete_profile` 函数的实现位置？",
            _scope(endpoint.blob_name, helper.blob_name),
        )

        assert llm_reranker.calls == 0
        assert [result.path for result in results] == [endpoint.path, helper.path]

    async def test_multi_facet_query_recalls_and_fuses_each_facet(self):
        full = "Find authentication middleware. Trace credential reload."
        store = FakeSearchStore()
        store.hits_by_query = {
            full: [_hit("src/auth.py", 0.9)],
            "Find authentication middleware": [_hit("src/auth.py", 0.8)],
            "Trace credential reload": [_hit("src/credentials.py", 0.7)],
        }
        embedder = FakeEmbedder()
        pipe = RetrievalPipeline(
            embedder=embedder,
            store=store,
            settings=_settings(
                confidence_floor=0.0,
                final_select_k=10,
                query_decomposition_enabled=True,
                query_max_queries=3,
            ),
        )

        results = await pipe.search(full)

        assert store.queries == [
            full,
            "Find authentication middleware",
            "Trace credential reload",
        ]
        assert [hit.path for hit in results] == [
            "src/auth.py",
            "src/credentials.py",
        ]
        assert len(embedder.queries) == 3

    async def test_duplicate_planned_query_reuses_one_vector(self):
        class DuplicatePlanner:
            def plan(self, query: str) -> list[str]:
                return [query, query]

        store = FakeSearchStore([_hit("src/a.py", 0.9)])
        embedder = FakeEmbedder()
        pipe = RetrievalPipeline(
            embedder=embedder,
            store=store,
            query_planner=DuplicatePlanner(),
            settings=_settings(confidence_floor=0.0, final_select_k=10),
        )

        await pipe.search("same query")

        assert store.queries == ["same query", "same query"]
        assert embedder.queries == ["same query"]

    async def test_query_decomposition_can_be_disabled(self):
        store = FakeSearchStore([_hit("src/a.py", 0.9)])
        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=store,
            settings=_settings(
                confidence_floor=0.0,
                query_decomposition_enabled=False,
            ),
        )

        await pipe.search("First repository concern. Second repository concern.")

        assert store.queries == ["First repository concern. Second repository concern."]


class TestRerankRouting:
    """授权与路由分离：RERANK_ENABLED 决定能不能调，policy 决定这次调不调。"""

    class CountingReranker:
        def __init__(self):
            self.calls = 0

        async def rerank(self, query, hits):
            self.calls += 1
            return list(reversed(hits))

    @staticmethod
    def _exact_pipe(reranker, **overrides):
        endpoint = SearchHit(
            blob_name="a" * 64,
            path="src/commands/profile.rs",
            content="pub fn delete_profile() {}",
            score=1.0,
        )
        helper = SearchHit(
            blob_name="b" * 64,
            path="src/services/profile.rs",
            content="fn delete_profile_helper() {}",
            score=0.95,
        )
        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=FakeSearchStore(),
            exact_store=FakeExactSearchStore([endpoint, helper]),
            reranker=reranker,
            settings=_settings(confidence_floor=0.0, final_select_k=10, **overrides),
        )
        return pipe, _scope(endpoint.blob_name, helper.blob_name)

    async def test_adaptive_skips_dedicated_reranker_on_exact_definition(self):
        reranker = self.CountingReranker()
        pipe, scope = self._exact_pipe(reranker)
        audit = RetrievalAudit()

        results = await pipe.search("`delete_profile` 在哪里定义？", scope, audit=audit)

        assert reranker.calls == 0
        assert audit.rerank_route == "skip:exact_definition"
        assert audit.head_slots == 2
        assert results[0].path == "src/commands/profile.rs"

    async def test_always_policy_still_calls_dedicated_reranker(self):
        reranker = self.CountingReranker()
        pipe, scope = self._exact_pipe(reranker, rerank_policy="always")
        audit = RetrievalAudit()

        results = await pipe.search("`delete_profile` 在哪里定义？", scope, audit=audit)

        assert reranker.calls == 1
        assert audit.rerank_route == "dedicated"
        assert results[0].path == "src/commands/profile.rs"

    async def test_adaptive_reranks_embedding_only_path_evidence(self):
        reranker = self.CountingReranker()
        hits = [_hit("docs/CHANGES.rst", 0.4), _hit("src/version.py", 0.5)]
        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=FakeSearchStore(hits),
            path_store=FakePathStore(
                [PathSearchResult("docs/CHANGES.rst", "a" * 64, 0.9)]
            ),
            reranker=reranker,
            settings=_settings(confidence_floor=0.0, final_select_k=10),
        )
        audit = RetrievalAudit()

        await pipe.search("Where is the CHANGES.rst file?", audit=audit)

        assert reranker.calls == 1
        assert audit.rerank_route == "dedicated"

    async def test_semantic_query_uses_dedicated_reranker(self):
        reranker = self.CountingReranker()
        hits = [_hit("src/a.py", 0.5), _hit("src/b.py", 0.9)]
        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=FakeSearchStore(hits),
            reranker=reranker,
            settings=_settings(confidence_floor=0.0, final_select_k=10),
        )
        audit = RetrievalAudit()

        await pipe.search(
            "how does the retry logic recover from failures?", audit=audit
        )

        assert reranker.calls == 1
        assert audit.rerank_route == "dedicated"

    async def test_unauthorized_reranker_is_reported_as_not_enabled(self):
        hits = [_hit("src/a.py", 0.5), _hit("src/b.py", 0.9)]
        pipe = RetrievalPipeline(
            embedder=FakeEmbedder(),
            store=FakeSearchStore(hits),
            settings=_settings(
                confidence_floor=0.0, final_select_k=10, rerank_policy="always"
            ),
        )
        audit = RetrievalAudit()

        await pipe.search(
            "how does the retry logic recover from failures?", audit=audit
        )

        assert audit.rerank_route == "skip:no_reranker_enabled"
