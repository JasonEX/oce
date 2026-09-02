"""RetrievalPipeline 领域服务 - 检索编排

流程：
    embed_query → store.search（dense 向量检索）
    → 精确标识符召回 → 多查询结果融合 → 源码优先/可选置信度过滤
    → 专用 rerank → 可选 LLM 语义 rerank → select（最终 K 条）

rerank 解决「单篇多相关」，select 解决「这一组够全且不冗余」，职责不同。
关闭查询分解、使用 Noop reranker 和自定义 TopK selector 时，可退化为传统 Top-K。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from typing import TYPE_CHECKING

from loguru import logger

from oce.domain.services.embedder import Embedder
from oce.domain.services.path_search import PathContentStore, PathSearchStore
from oce.domain.services.query_classifier import (
    QueryIntent,
    classify_query_intent,
    extract_code_identifiers,
    should_use_path_index,
)
from oce.domain.services.query_planner import HeuristicQueryPlanner, QueryPlanner
from oce.domain.services.reranker import Reranker
from oce.domain.services.retrieval_strategy import get_strategy, plan_rerank
from oce.domain.services.search import (
    ExactSearchStore,
    SearchHit,
    SearchHitKey,
    SearchScope,
    SearchStore,
    search_hit_key,
)
from oce.domain.services.selector.coverage_selector import CoverageSelector
from oce.domain.services.selector.protocols import SelectionMode, Selector
from oce.domain.services.selector.topk_selector import TopKSelector
from oce.shared.config.settings import RetrievalSettings
from oce.shared.metrics import RetrievalAudit

if TYPE_CHECKING:
    from oce.domain.services.llm.rewriter import QueryRewriter


@contextmanager
def _noop_stage(_name: str) -> Iterator[None]:
    """audit=None 时的空计时上下文：不测量、零副作用。"""
    yield


def source_priority_factor(path: str) -> float:
    """源码优先先验：文档/测试类路径乘性降权，普通源码 1.0。

    与旧 retrieval/ranking.py 的规则集对齐（简化版，可注入自定义函数）。
    """
    p = path.replace("\\", "/").lower()
    name = p.rsplit("/", 1)[-1]
    stem = name.split(".", 1)[0]

    # 法律文件最重降权
    if stem in {"license", "notice", "copying"}:
        return 0.1
    # 主 README 显式不降权，多语言 README 降权
    if stem == "readme":
        return 1.0
    if stem.startswith("readme"):
        return 0.2
    # 文档目录 / 文档扩展
    if "/docs/" in f"/{p}" or p.endswith((".md", ".rst", ".txt")):
        return 0.5
    if (
        "/tests/" in f"/{p}"
        or name.startswith("test_")
        or name == "conftest.py"
        or ".test." in name
        or ".spec." in name
    ):
        return 0.6
    if name in {"index.ts", "index.tsx", "index.js", "index.jsx", "types.ts"}:
        return 0.85
    return 1.0


def neutral_priority_factor(_path: str) -> float:
    """文档中立的优先级因子：恒为 1.0。

    用于关闭 source priority，以及「XX 文件在哪里」类路径查询：任何文件类型都可能
    是目标（文档、配置、测试、license 都在找文件语义内），「源码优先」先验不成立，
    排序完全交由路径 boost + 内容分数 + rerank 决定。
    """
    return 1.0


class RetrievalPipeline:
    """检索管道：编排检索全流程"""

    def __init__(
        self,
        *,
        embedder: Embedder,
        store: SearchStore,
        settings: RetrievalSettings,
        reranker: Reranker | None = None,
        llm_reranker: Reranker | None = None,
        query_rewriter: QueryRewriter | None = None,
        path_store: PathSearchStore | None = None,
        path_content_store: PathContentStore | None = None,
        exact_store: ExactSearchStore | None = None,
        selector: Selector | None = None,
        query_planner: QueryPlanner | None = None,
        priority_factor: Callable[[str], float] | None = None,
        rerank_window: int | None = None,
    ) -> None:
        self.embedder = embedder
        self.store = store
        # None 表示未授权专用 reranker；决策与审计据此区分「未启用」和「按策略跳过」。
        self.reranker = reranker
        self.llm_reranker = llm_reranker
        # LLM reranker 一次能看到的候选数；None 表示没有窗口限制。exact 命中必须
        # 落在窗口内才有机会被重排，否则调用链查询的定义会被语义候选挤出。
        self.rerank_window = rerank_window
        self.query_rewriter = query_rewriter
        self.path_store = path_store
        self.path_content_store = path_content_store
        self.settings = settings
        self.exact_store = exact_store
        self.priority_factor = priority_factor or (
            source_priority_factor
            if self.settings.source_priority_enabled
            else neutral_priority_factor
        )
        self.query_planner = query_planner or HeuristicQueryPlanner(
            max_queries=(
                self.settings.query_max_queries
                if self.settings.query_decomposition_enabled
                else 1
            ),
            min_facet_chars=self.settings.query_min_facet_chars,
        )
        if selector is not None:
            self.selector = selector
        elif self.settings.coverage_selection_enabled:
            self.selector = CoverageSelector(
                max_per_path=self.settings.max_chunks_per_path,
                focused_max_per_path=self.settings.focused_max_chunks_per_path,
                max_chars=self.settings.max_context_chars,
                overlap_threshold=self.settings.overlap_threshold,
            )
        else:
            self.selector = TopKSelector()

    async def search(
        self,
        query: str,
        scope: SearchScope | None = None,
        *,
        audit: RetrievalAudit | None = None,
    ) -> list[SearchHit]:
        """执行一次检索，返回最终命中列表（按融合分降序）。

        Args:
            query: 查询文本
            scope: 已解析的工作集；None 仅供内部测试使用，表示不过滤。
            audit: 可选的阶段耗时收集容器；为 None 时不打点、零开销。
        """
        stage = audit.stage if audit is not None else _noop_stage
        allowed_blob_names = scope.blob_names if scope is not None else None
        if audit is not None:
            audit.scope_size = (
                len(allowed_blob_names) if allowed_blob_names is not None else None
            )

        # None 表示不过滤，空集合表示无可搜索内容
        if allowed_blob_names is not None and len(allowed_blob_names) == 0:
            return []

        # 路由完全由可测试的确定性信号决定；模型只参与显式的 rewrite/rerank。
        intent = classify_query_intent(query)
        strategy = get_strategy(intent)
        logger.debug("Query intent: {}, strategy: {}", intent.value, strategy)
        # 路径索引回答「哪个文件」，内容索引回答「文件里哪一段」；两者只在
        # 文件定位类查询上并行召回，再按 chunk 粒度合并。
        use_path_index = self.path_store is not None and (
            strategy.enable_path_index or should_use_path_index(query, intent)
        )
        if audit is not None:
            audit.intent = intent.value
            audit.path_boosted = use_path_index

        # QueryRewriter 内部已容错：失败时返回原查询，不会抛到这里。
        queries_to_search = [query]
        if strategy.enable_query_rewrite and self.query_rewriter is not None:
            with stage("rewrite"):
                rewritten_queries = await self.query_rewriter.rewrite(query)
            if rewritten_queries:
                queries_to_search = rewritten_queries

        planned_queries = self._plan_queries(queries_to_search)
        # 路径索引用原查询 + 改写变体分别检索：中文查询直接 embedding 常匹配不到
        # 英文路径文档，改写变体（含文件名如 CHANGES.rst）才能命中。
        path_queries = (
            tuple(dict.fromkeys((query, *queries_to_search))) if use_path_index else ()
        )
        with stage("embed"):
            query_vectors = await self._embed_query_vectors(
                [*path_queries, *(item[0] for item in planned_queries)]
            )

        # Exact SQL, dense vector I/O and the path index are independent after
        # routing and rewrite, so they run together; fusion order is unchanged.
        async def recall_dense() -> tuple[list[SearchHit], Exception | None]:
            try:
                with stage("dense"):
                    result_lists = await asyncio.gather(
                        *(
                            self._recall_with_vector(
                                query_vectors[planned_query],
                                allowed_blob_names,
                                num_queries,
                            )
                            for planned_query, num_queries in planned_queries
                        )
                    )
                return (self._fuse(list(result_lists)) if result_lists else []), None
            except Exception as exc:
                if not use_path_index:
                    raise
                logger.warning("Content search failed: {}", type(exc).__name__)
                return [], exc

        async def recall_exact() -> list[SearchHit]:
            with stage("exact"):
                return await self._recall_exact(query, scope)

        async def recall_paths() -> dict[str, float]:
            if not use_path_index:
                return {}
            with stage("path"):
                return await self._recall_paths(
                    [query_vectors[variant] for variant in path_queries],
                    allowed_blob_names,
                )

        (content_hits, content_error), exact_hits, path_scores = await asyncio.gather(
            recall_dense(),
            recall_exact(),
            recall_paths(),
        )

        with stage("fuse"):
            hits = self._merge_exact_hits(intent, exact_hits, content_hits)
            if path_scores:
                hits = await self._merge_path_and_content(path_scores, hits)
            elif use_path_index:
                logger.info("No path results, using content-only")
            # 路径索引失败且内容检索也失败：没有任何候选时才把内容错误抛出。
            if not hits and content_error is not None:
                raise content_error
        if not hits:
            return []

        return await self._rank_and_select(
            query,
            hits,
            intent=intent,
            selection_mode=strategy.selection_mode,
            has_exact_hits=bool(exact_hits),
            has_path_hits=bool(path_scores),
            # 路径类查询使用文档中立先验（不降权 .rst/.md/.txt）。
            priority_factor=neutral_priority_factor if use_path_index else None,
            audit=audit,
        )

    async def _rank_and_select(
        self,
        query: str,
        hits: list[SearchHit],
        *,
        intent: QueryIntent,
        selection_mode: SelectionMode,
        has_exact_hits: bool = False,
        has_path_hits: bool = False,
        priority_factor: Callable[[str], float] | None = None,
        audit: RetrievalAudit | None = None,
    ) -> list[SearchHit]:
        """Apply one candidate-preserving rerank state machine before selection."""
        stage = audit.stage if audit is not None else _noop_stage

        # Static source priors prepare the candidate order. Model rerankers run
        # afterwards, so their returned order cannot be silently overwritten.
        hits = self._apply_source_priority(hits, priority_factor=priority_factor)
        # This optional floor belongs to recall, before model scores can enter the
        # list. Dedicated relevance scores, dense cosine, and RRF are not calibrated
        # to a shared scale; filtering their mixture after reranking is undefined.
        hits = self._apply_confidence_floor(hits, priority_factor=priority_factor)

        decision = plan_rerank(
            intent,
            len(hits),
            has_exact_hits=has_exact_hits,
            has_path_hits=has_path_hits,
            dedicated_enabled=self.reranker is not None,
            llm_enabled=self.llm_reranker is not None,
            dedicated_policy=self.settings.rerank_policy,
            llm_policy=self.settings.llm_rerank_policy,
        )
        if audit is not None:
            audit.rerank_route = decision.route
        if decision.dedicated and self.reranker is not None:
            with stage("rerank"):
                hits = await self.reranker.rerank(query, hits)
        if decision.llm and self.llm_reranker is not None:
            with stage("llm_rerank"):
                hits = await self.llm_reranker.rerank(query, hits)

        with stage("select"):
            return await self.selector.select(
                hits,
                self.settings.final_select_k,
                mode=selection_mode,
            )

    def _plan_queries(self, queries: Sequence[str]) -> list[tuple[str, int]]:
        planned: list[tuple[str, int]] = []
        for query in queries:
            facets = self.query_planner.plan(query)
            count = len(facets)
            planned.extend((facet, count) for facet in facets)
        return planned

    async def _embed_query_vectors(
        self,
        queries: Sequence[str],
    ) -> dict[str, list[float]]:
        unique_queries = tuple(dict.fromkeys(queries))
        vectors = await asyncio.gather(
            *(self.embedder.embed_query(query) for query in unique_queries)
        )
        return dict(zip(unique_queries, vectors, strict=True))

    async def _recall_with_vector(
        self,
        query_vector: list[float],
        allowed_blob_names: set[str] | frozenset[str] | None,
        num_queries: int = 1,
    ) -> list[SearchHit]:
        # 动态调整召回量：单查询用 default_top_k，多查询用 per_query_top_k
        top_k = (
            self.settings.default_top_k
            if num_queries == 1
            else self.settings.per_query_top_k
        )

        return await self.store.search(
            query_vector=query_vector,
            allowed_blob_names=(
                sorted(allowed_blob_names) if allowed_blob_names is not None else None
            ),
            top_k=top_k,
            vector_threshold=self.settings.vector_threshold,
        )

    async def _recall_exact(
        self,
        query: str,
        scope: SearchScope | None,
    ) -> list[SearchHit]:
        if (
            not self.settings.exact_enabled
            or self.exact_store is None
            or scope is None
            or not scope.blob_names
        ):
            return []
        identifiers = extract_code_identifiers(query)
        if not identifiers:
            return []
        try:
            return await self.exact_store.search_exact(
                identifiers=identifiers,
                scope=scope,
                top_k=self.settings.default_top_k,
            )
        except Exception as exc:
            logger.warning(
                "Exact identifier recall failed; using semantic candidates: {}", exc
            )
            return []

    def _merge_exact_hits(
        self,
        intent: QueryIntent,
        exact_hits: list[SearchHit],
        semantic_hits: list[SearchHit],
    ) -> list[SearchHit]:
        if intent == QueryIntent.CALL_CHAIN and semantic_hits:
            semantic_keys = {search_hit_key(hit) for hit in semantic_hits}
            exact_only = [
                hit for hit in exact_hits if search_hit_key(hit) not in semantic_keys
            ]
            candidate_window = min(
                self.settings.default_top_k,
                self.rerank_window or self.settings.default_top_k,
            )
            reserved = min(len(exact_only), max(1, candidate_window // 3))
            semantic_slots = max(candidate_window - reserved, 1)
            anchor_index = min(semantic_slots, len(semantic_hits)) - 1
            anchor_score = semantic_hits[anchor_index].score
            exact_only = [
                replace(hit, score=min(hit.score, anchor_score))
                for hit in exact_only[:reserved]
            ]
            merged = [*semantic_hits, *exact_only]
            merged.sort(key=lambda hit: hit.score, reverse=True)
            return merged[: self.settings.default_top_k]

        merged: list[SearchHit] = []
        positions: dict[SearchHitKey, int] = {}
        for hit in [*exact_hits, *semantic_hits]:
            key = search_hit_key(hit)
            position = positions.get(key)
            if position is None:
                positions[key] = len(merged)
                merged.append(hit)
            elif hit.score > merged[position].score:
                merged[position] = hit
        merged.sort(key=lambda hit: hit.score, reverse=True)
        return merged[: self.settings.default_top_k]

    def _fuse(self, result_lists: list[list[SearchHit]]) -> list[SearchHit]:
        if len(result_lists) == 1:
            return result_lists[0]

        rrf_k = self.settings.rrf_k  # 统一使用 rrf_k
        weights = [1.0] + [self.settings.query_facet_weight] * (len(result_lists) - 1)
        max_score = sum(weight / (rrf_k + 1) for weight in weights)
        scores: dict[SearchHitKey, float] = {}
        hits_by_key: dict[SearchHitKey, SearchHit] = {}
        first_seen: dict[SearchHitKey, int] = {}
        ordinal = 0
        for weight, hits in zip(weights, result_lists, strict=True):
            for rank, hit in enumerate(hits, 1):
                key = search_hit_key(hit)
                if key not in first_seen:
                    first_seen[key] = ordinal
                    ordinal += 1
                    hits_by_key[key] = hit
                scores[key] = scores.get(key, 0.0) + weight / (rrf_k + rank)

        keys = sorted(scores, key=lambda key: (-scores[key], first_seen[key]))
        return [
            replace(hits_by_key[key], score=scores[key] / max_score)
            for key in keys[: self.settings.default_top_k]
        ]

    def _apply_source_priority(
        self,
        hits: list[SearchHit],
        priority_factor: Callable[[str], float] | None = None,
    ) -> list[SearchHit]:
        """按 score × 路径惩罚因子稳定重排（只重排，不改 rerank 决策）。

        普通查询默认用 self.priority_factor（源码优先）；路径类查询可传
        文档中立的 factor（neutral_priority_factor）。
        """
        factor = priority_factor or self.priority_factor
        return sorted(
            hits,
            key=lambda h: h.score * factor(h.path),
            reverse=True,
        )

    def _apply_confidence_floor(
        self,
        hits: list[SearchHit],
        priority_factor: Callable[[str], float] | None = None,
    ) -> list[SearchHit]:
        """逐条按有效分（score × penalty）剔除低于门槛的弱匹配"""
        factor = priority_factor or self.priority_factor
        floor = self.settings.confidence_floor
        return [h for h in hits if h.score * factor(h.path) >= floor]

    async def _recall_paths(
        self,
        query_vectors: Sequence[list[float]],
        allowed_blob_names: frozenset[str] | None,
    ) -> dict[str, float]:
        """Best path score per blob over every query variant; failures degrade to none."""
        path_scores: dict[str, float] = {}
        if self.path_store is None:
            return path_scores
        blob_filter = list(allowed_blob_names) if allowed_blob_names else None
        try:
            result_lists = await asyncio.gather(
                *(
                    self.path_store.search_paths(
                        query_vector=vector,
                        allowed_blob_names=blob_filter,
                        top_k=self.settings.path_top_k,
                    )
                    for vector in query_vectors
                )
            )
        except Exception as exc:
            logger.warning(
                "Path index search failed: {}; falling back to content-only",
                type(exc).__name__,
            )
            return path_scores
        for path_results in result_lists:
            for result in path_results:
                if result.score > path_scores.get(result.blob_name, float("-inf")):
                    path_scores[result.blob_name] = result.score
        logger.info("Path index returned {} results", len(path_scores))
        return path_scores

    async def _merge_path_and_content(
        self,
        path_scores: dict[str, float],
        content_hits: list[SearchHit],
    ) -> list[SearchHit]:
        """按 chunk 粒度合并路径命中与内容命中。

        旧实现按 path 去重并让路径结果优先，导致路径索引越准、正确 chunk 越会被
        丢弃：「`add_provider` 在哪个文件定义？」路径索引选对 provider.rs 后，
        内容索引里含该函数的 chunk 因同属该文件而被整块剔除。
        """
        weight = self.settings.path_boost_weight

        merged: list[SearchHit] = []
        covered: set[str] = set()
        for hit in content_hits:
            boost = path_scores.get(hit.blob_name)
            if boost is None:
                merged.append(hit)
                continue
            covered.add(hit.blob_name)
            merged.append(replace(hit, score=hit.score + weight * boost))

        # 内容检索完全没覆盖到的文件才回填首个 chunk，保住纯文件名查询的召回
        missing = [name for name in path_scores if name not in covered]
        if missing:
            try:
                merged.extend(await self._fetch_content_for_paths(missing, path_scores))
            except Exception as exc:
                if not merged:
                    raise
                logger.error(
                    "Failed to fetch content for path-only hits: {}; "
                    "using content candidates",
                    type(exc).__name__,
                )

        logger.info(
            "Merged {} content hits with {} path hits (boosted={}, backfilled={})",
            len(content_hits),
            len(path_scores),
            len(covered),
            len(missing),
        )
        merged.sort(key=lambda hit: hit.score, reverse=True)
        return merged

    async def _fetch_content_for_paths(
        self,
        blob_names: list[str],
        path_scores: dict[str, float],
    ) -> list[SearchHit]:
        """
        为路径索引结果获取实际内容

        仅用于内容索引完全没召回的文件：取首个 chunk 作为代表，让纯文件名查询
        至少能命中目标文件。
        """
        if self.path_content_store is None:
            return []
        hits = await self.path_content_store.get_representative_chunks(blob_names)
        return [replace(hit, score=path_scores.get(hit.blob_name, 0.0)) for hit in hits]
