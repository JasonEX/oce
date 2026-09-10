"""RetrievalPipeline 应用服务 - 检索状态机

一次检索是一条固定顺序的状态转移，每个阶段只读写 ``RetrievalState`` 上属于它的字段：

    route   查询 → intent / strategy / QueryEvidence（标识符、文件名、路径、报错短语、词元）
    plan    可选 LLM 改写 + 句子级 facet 分解 + 查询向量
    recall  dense | exact | 按意图 lexical | path（embedding 路径索引 + SQL 精确路径查找），并行
    fuse    dense/lexical 按 RRF 融合 → 合并 exact → 路径 boost/回填
    prior   源码先验 × 工作集增量先验 → 保护确定性头部
    rerank  plan_rerank 决策 → 专用 reranker → chat-LLM reranker（均保留候选集）
    select  focused / coverage 选择（数量软上限、字符硬预算）
    expand  同文件相邻片段合并 → 按意图与剩余预算附带关系小节：被引用定义、
            调用方、实现/子类、覆盖测试、转出入口（各自独立槽位与字符上限）

rerank 解决「单篇多相关」，select 解决「这一组够全且不冗余」，expand 解决「拿到的
片段引用了什么」。关闭对应开关时每个阶段都退化为恒等变换。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from time import perf_counter
from typing import TYPE_CHECKING

from loguru import logger

from oce.application.call_chain import (
    resolve_endpoints,
    trace_callees,
    trace_callers,
    trace_path,
)
from oce.domain.services.embedder import Embedder
from oce.domain.services.evidence_pack import (
    SectionInput,
    assemble_sections,
    merge_adjacent_hits,
)
from oce.domain.services.lexical import lexical_tokens
from oce.domain.services.path_search import PathContentStore, PathSearchStore
from oce.domain.services.query_classifier import (
    QueryIntent,
    QueryRoute,
    route_query,
)
from oce.domain.services.query_evidence import QueryEvidence, extract_query_evidence
from oce.domain.services.query_planner import HeuristicQueryPlanner, QueryPlanner
from oce.domain.services.ranking import (
    HeadEvidence,
    apply_priors,
    neutral_priority_factor,
    promote_heads,
    source_heads,
    source_priority_factor,
    structural_heads,
)
from oce.domain.services.related_definitions import (
    DefinitionCandidates,
    select_related_definitions,
)
from oce.domain.services.relations import (
    RelatedOccurrence,
    RelationStore,
)
from oce.domain.services.reranker import Reranker
from oce.domain.services.retrieval_strategy import (
    RerankDecision,
    RetrievalStrategy,
    get_strategy,
    plan_rerank,
)
from oce.domain.services.search import (
    DefinitionHit,
    ExactSearchStore,
    HitRole,
    LexicalSearchStore,
    PathLookupStore,
    SearchHit,
    SearchHitKey,
    SearchScope,
    SearchStore,
    search_hit_key,
)
from oce.domain.services.selector.coverage_selector import CoverageSelector
from oce.domain.services.selector.protocols import SelectionMode, Selector
from oce.domain.services.selector.topk_selector import TopKSelector
from oce.domain.services.symbol_resolution import (
    _IDENTIFIER_NOISE,
    _QUALIFIER_SEPARATORS,
    _frame_matches,
    _leaf,
    _mine_identifiers,
    _word_in,
    mentions_requested_name,
    order_by_comentions,
    order_by_signature_comentions,
    resolve_qualified_definitions,
    resolve_qualified_hits,
    split_qualified_identifiers,
)
from oce.domain.services.symbols import (
    CALL_KIND,
    DEFINITION_KINDS,
    HEADER_KINDS,
    USE_SITE_KINDS,
)
from oce.shared.config.settings import RetrievalSettings
from oce.shared.metrics import RetrievalAudit

if TYPE_CHECKING:
    from oce.domain.services.llm.rewriter import QueryRewriter


@contextmanager
def _noop_stage(_name: str) -> Iterator[None]:
    """audit=None 时的空计时上下文：不测量、零副作用。"""
    yield


def _swallow_task_result(task: asyncio.Task[object]) -> None:
    """Consume the outcome of a released background task so nothing is left pending."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.debug("Released query embedding failed: {}", type(exc).__name__)


# Avoid querying declarations for a budget too small to show a useful relation.
_MIN_RELATED_BUDGET = 1_000


@dataclass
class RetrievalState:
    """Mutable record of one retrieval; each stage owns the fields it fills."""

    query: str
    scope: SearchScope | None
    audit: RetrievalAudit | None = None

    # route
    evidence: QueryEvidence | None = None
    # Names the structural lanes look up: every query identifier plus the leaf
    # of each qualified one (``Session.get`` -> ``get``); ``qualifiers`` maps a
    # leaf to the scopes the request pinned it to.
    lookup_identifiers: tuple[str, ...] = ()
    qualifiers: dict[str, tuple[str, ...]] = field(default_factory=dict)
    route: QueryRoute = field(default_factory=QueryRoute)
    strategy: RetrievalStrategy = field(default_factory=RetrievalStrategy)
    use_path_index: bool = False

    # plan
    queries: list[str] = field(default_factory=list)
    planned: list[tuple[str, int]] = field(default_factory=list)
    path_queries: tuple[str, ...] = ()
    vectors: dict[str, list[float]] = field(default_factory=dict)
    embed_error: Exception | None = None
    # The remote embedding round trip runs as a task so the SQL lanes can
    # answer first; ``dense_route`` records whether it was awaited.
    embed_task: asyncio.Task[tuple[dict[str, list[float]], int]] | None = None
    dense_route: str | None = None
    # Stage timings of the vector lanes, committed to the audit only when
    # their result is used; a lane that raced ahead and was dropped leaves
    # no trace of work the answer never depended on.
    vector_stages: dict[str, int] = field(default_factory=dict)

    # recall
    dense: list[SearchHit] = field(default_factory=list)
    dense_error: Exception | None = None
    exact: list[SearchHit] = field(default_factory=list)
    # Definition/endpoint chunks of the queried identifiers. Reference queries
    # recall every occurrence kind, so the declaration must be told apart
    # from the use sites the question actually asks for.
    definitions: list[SearchHit] = field(default_factory=list)
    # Every requested symbol must resolve before SQL is decisive. Definitions
    # of incidental parameter types do not satisfy that requirement.
    primary_definition_found: bool = False
    # Reference queries: chunks that call or extend the queried identifiers,
    # i.e. deterministic use sites as opposed to imports or the declaration.
    use_sites: list[SearchHit] = field(default_factory=list)
    # Explicit implementation requests reuse the same bounded inherit facts
    # for head ordering and relation assembly after the primary selection.
    implementations: list[RelatedOccurrence] = field(default_factory=list)
    # Call-chain queries: the declarations of each named symbol, in query
    # order, so "how does A reach B" can start at A and stop at B.
    endpoints: list[tuple[str, list[DefinitionHit]]] = field(default_factory=list)
    lexical: list[SearchHit] = field(default_factory=list)
    # Compound requests: definition chunks of the identifiers the text names
    # that are declared in few enough places to be unambiguous.
    anchors: list[SearchHit] = field(default_factory=list)
    path_scores: dict[str, float] = field(default_factory=dict)
    lookup_scores: dict[str, float] = field(default_factory=dict)

    # fuse / prior / rerank / select / expand
    candidates: list[SearchHit] = field(default_factory=list)
    decision: RerankDecision | None = None
    selected: list[SearchHit] = field(default_factory=list)
    related: list[SearchHit] = field(default_factory=list)

    @property
    def identifiers(self) -> tuple[str, ...]:
        mentions = self.evidence.identifiers if self.evidence else ()
        return tuple(dict.fromkeys((*self.route.targets, *mentions)))

    @property
    def allowed_blob_names(self) -> frozenset[str] | None:
        return self.scope.blob_names if self.scope is not None else None

    def stage(self, name: str):
        return self.audit.stage(name) if self.audit is not None else _noop_stage(name)

    @contextmanager
    def vector_stage(self, name: str) -> Iterator[None]:
        if self.audit is None:
            yield
            return
        start = perf_counter()
        try:
            yield
        finally:
            elapsed = int((perf_counter() - start) * 1000)
            self.vector_stages[name] = self.vector_stages.get(name, 0) + elapsed


@dataclass(frozen=True)
class _RelationLane:
    role: HitRole
    fetch: Callable[[Sequence[str]], Awaitable[list[RelatedOccurrence]]]
    max_items: int
    max_chars: int


async def _no_hits() -> list[SearchHit]:
    return []


async def _no_occurrences() -> list[RelatedOccurrence]:
    return []


async def _no_definitions() -> list[DefinitionHit]:
    return []


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
        path_lookup_store: PathLookupStore | None = None,
        exact_store: ExactSearchStore | None = None,
        relation_store: RelationStore | None = None,
        lexical_store: LexicalSearchStore | None = None,
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
        self.path_lookup_store = path_lookup_store
        self.settings = settings
        self.exact_store = exact_store
        self.relation_store = relation_store
        self.lexical_store = lexical_store
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
                focused_max_chars=self.settings.focused_max_context_chars,
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
        """执行一次检索，返回主结果（按最终排序）加二跳定义摘要（role=related）。

        Args:
            query: 查询文本
            scope: 已解析的工作集；None 仅供内部测试使用，表示不过滤。
            audit: 可选的阶段耗时收集容器；为 None 时不打点、零开销。
        """
        state = RetrievalState(query=query, scope=scope, audit=audit)
        if audit is not None:
            audit.scope_size = (
                len(state.allowed_blob_names)
                if state.allowed_blob_names is not None
                else None
            )
        # None 表示不过滤，空集合表示无可搜索内容
        if state.allowed_blob_names is not None and not state.allowed_blob_names:
            return []

        self._route(state)
        # SQL lanes need only routing, so they overlap the remote query
        # embedding instead of waiting behind it.
        sql_lanes = self._start_sql_lanes(state)
        try:
            await self._plan(state)
            await self._recall(state, sql_lanes)
        except BaseException:
            for task in sql_lanes:
                task.cancel()
            # Merely cancelling background tasks leaves their exceptions and
            # database contexts pending. Drain every lane before propagating
            # planning failures or caller cancellation. The embedding request
            # is released instead of cancelled (see ``_release_embedding``).
            await asyncio.gather(*sql_lanes, return_exceptions=True)
            self._release_embedding(state)
            raise
        await self._fuse(state)
        if not state.candidates:
            return []
        await self._rank(state)
        await self._select(state)
        await self._expand(state)
        return [*state.selected, *state.related]

    # ── route ────────────────────────────────────────────────────────────

    def _route(self, state: RetrievalState) -> None:
        """路由完全由可测试的确定性信号决定；模型只参与显式的 rewrite/rerank。"""
        state.evidence = extract_query_evidence(state.query)
        state.route = route_query(state.query, state.evidence)
        state.lookup_identifiers, state.qualifiers = split_qualified_identifiers(
            state.identifiers
        )
        state.strategy = get_strategy(state.route.intent)
        if state.route.intent == QueryIntent.REFERENCE and not state.route.targets:
            # A request for tests of a behavior identifies an evidence kind,
            # but no bounded symbol target. It still needs semantic coverage.
            state.strategy = replace(
                state.strategy, selection_mode=SelectionMode.COVERAGE
            )
        logger.debug(
            "Query intent: {}, strategy: {}", state.route.intent.value, state.strategy
        )
        # 路径索引回答「哪个文件」，内容索引回答「文件里哪一段」；两者只在
        # 文件定位类查询上并行召回，再按 chunk 粒度合并。
        state.use_path_index = self.path_store is not None and (
            state.route.path_requested
        )
        if state.audit is not None:
            state.audit.intent = state.route.intent.value
            state.audit.path_boosted = state.use_path_index

    # ── plan ─────────────────────────────────────────────────────────────

    async def _plan(self, state: RetrievalState) -> None:
        # QueryRewriter 内部已容错：失败时返回原查询，不会抛到这里。
        state.queries = [state.query]
        if state.strategy.enable_query_rewrite and self.query_rewriter is not None:
            with state.stage("rewrite"):
                rewritten = await self.query_rewriter.rewrite(state.query)
            if rewritten:
                state.queries = list(rewritten)

        state.planned = self._plan_queries(state.queries)
        # 路径索引用原查询 + 改写变体分别检索：中文查询直接 embedding 常匹配不到
        # 英文路径文档，改写变体（含文件名如 CHANGES.rst）才能命中。
        state.path_queries = (
            tuple(dict.fromkeys((state.query, *state.queries)))
            if state.use_path_index
            else ()
        )
        # Started, not awaited: ``_recall`` decides whether the answer needs it.
        state.embed_task = asyncio.create_task(
            self._embed_query_vectors(
                [*state.path_queries, *(item[0] for item in state.planned)]
            )
        )

    @staticmethod
    def _release_embedding(state: RetrievalState) -> None:
        """Let an unneeded embedding request finish on its own.

        Cancelling an in-flight HTTP request mid-response can leave its pooled
        connection unreturned; after enough skips the client blocks on the
        pool and every later request times out. The request is cheap to let
        complete, it fills the query-vector cache, and nothing waits for it.
        """
        task = state.embed_task
        if task is None:
            return
        if task.done():
            _swallow_task_result(task)
            return
        task.add_done_callback(_swallow_task_result)

    async def _await_embedding(self, state: RetrievalState) -> None:
        if state.embed_task is None:
            return
        try:
            # Shielded: cancelling the vector lanes must not cancel the request.
            state.vectors, elapsed_ms = await asyncio.shield(state.embed_task)
        except Exception as exc:
            if not self._has_fallback_recall(state):
                raise
            logger.warning(
                "Query embedding failed; trying SQL retrieval: {}",
                type(exc).__name__,
            )
            state.embed_error = exc
            return
        state.vector_stages["embed"] = elapsed_ms

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
    ) -> tuple[dict[str, list[float]], int]:
        """Query vectors plus the wall time of the round trip in milliseconds."""
        started = perf_counter()
        unique_queries = tuple(dict.fromkeys(queries))
        vectors = await asyncio.gather(
            *(self.embedder.embed_query(query) for query in unique_queries)
        )
        elapsed_ms = int((perf_counter() - started) * 1000)
        return dict(zip(unique_queries, vectors, strict=True)), elapsed_ms

    # ── recall ───────────────────────────────────────────────────────────

    def _start_sql_lanes(
        self, state: RetrievalState
    ) -> tuple[asyncio.Task[object], ...]:
        """Exact, path lookup and routed lexical recall depend only on routing.

        They start before the query embedding round trip. Symbol/path queries
        defer lexical I/O until their structural operator misses; that keeps
        the common exact path fast without giving up the fallback.
        """
        eager_lexical = self._should_recall_lexical_eagerly(state)
        return (
            asyncio.create_task(self._recall_exact(state)),
            asyncio.create_task(self._recall_path_lookup(state)),
            asyncio.create_task(self._recall_lexical(state, routed=eager_lexical)),
            asyncio.create_task(self._recall_anchors(state)),
        )

    async def _recall(
        self,
        state: RetrievalState,
        sql_lanes: tuple[asyncio.Task[object], ...],
    ) -> None:
        """SQL lanes first; the embedding-backed lanes only when they are needed.

        Complete requested definitions or a matched path can make SQL decisive.
        One use site does not establish reference coverage, so those requests
        retain dense recall. The vector lanes run concurrently and are dropped
        when SQL proves the request decisive; otherwise they are awaited.
        """
        exact_task, lookup_task, lexical_task, anchor_task = sql_lanes
        vector_lanes = asyncio.create_task(self._recall_vector_lanes(state))
        try:
            (
                (state.exact, state.definitions, state.use_sites),
                state.lookup_scores,
                state.lexical,
                state.anchors,
            ) = await asyncio.gather(exact_task, lookup_task, lexical_task, anchor_task)
        except BaseException:
            vector_lanes.cancel()
            await asyncio.gather(vector_lanes, return_exceptions=True)
            raise
        reason = self._decisive_reason(state)
        if reason is not None:
            vector_lanes.cancel()
            await asyncio.gather(vector_lanes, return_exceptions=True)
            self._release_embedding(state)
            state.dense_route = f"skip:{reason}"
        else:
            (state.dense, state.dense_error), state.path_scores = await vector_lanes
            state.dense_route = (
                "dense"
                if state.dense_error is None
                else f"error:{type(state.dense_error).__name__}"
            )
            if state.audit is not None:
                for name, elapsed_ms in state.vector_stages.items():
                    state.audit.record(name, elapsed_ms)
        if state.audit is not None:
            state.audit.dense_route = state.dense_route
        if not state.lexical and self._should_recall_lexical_fallback(state):
            state.lexical = await self._recall_lexical(state, routed=True)

    async def _recall_vector_lanes(
        self, state: RetrievalState
    ) -> tuple[tuple[list[SearchHit], Exception | None], dict[str, float]]:
        await self._await_embedding(state)
        return await asyncio.gather(
            self._recall_dense(state), self._recall_paths(state)
        )

    def _decisive_reason(self, state: RetrievalState) -> str | None:
        """Structural evidence that makes vector recall unnecessary, or None.

        Binary facts only: the definition lane found the symbol, the SQL path
        lookup matched a file. Finding one reference does not establish
        coverage of the requested uses, so reference recall retains dense.
        """
        if not self.settings.decisive_skips_dense or state.embed_task is None:
            return None
        if state.route.intent == QueryIntent.SYMBOL and state.primary_definition_found:
            return "exact_definition"
        if (
            state.route.intent == QueryIntent.PATH
            and state.lookup_scores
            # The matched file needs a chunk to show; without the content
            # store only dense recall can supply one.
            and self.path_content_store is not None
        ):
            return "path_evidence"
        return None

    async def _recall_dense(
        self, state: RetrievalState
    ) -> tuple[list[SearchHit], Exception | None]:
        if state.embed_error is not None:
            return [], state.embed_error
        try:
            with state.vector_stage("dense"):
                result_lists = await asyncio.gather(
                    *(
                        self._recall_with_vector(
                            state.vectors[planned_query],
                            state.allowed_blob_names,
                            num_queries,
                        )
                        for planned_query, num_queries in state.planned
                    )
                )
            return self._fuse_lists(list(result_lists)), None
        except Exception as exc:
            if not state.use_path_index and not self._has_fallback_recall(state):
                raise
            logger.warning("Content search failed: {}", type(exc).__name__)
            return [], exc

    def _has_fallback_recall(self, state: RetrievalState) -> bool:
        """Whether another operator can still answer when dense recall fails."""
        evidence = state.evidence
        return (
            evidence is not None
            and state.scope is not None
            and (
                (self.exact_store is not None and bool(state.identifiers))
                and self.settings.exact_enabled
                or self._can_recall_lexical(state)
                or (
                    self.path_lookup_store is not None
                    and self.path_content_store is not None
                    and self.settings.path_lookup_enabled
                    and evidence.has_path_evidence
                )
            )
        )

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
        self, state: RetrievalState
    ) -> tuple[list[SearchHit], list[SearchHit], list[SearchHit]]:
        """``(occurrences, definitions, use_sites)`` for the query identifiers.

        Reference questions want every occurrence kind including imports and
        additionally need to know which of those chunks declare the symbol and
        which call or extend it; everything else asks for the structural
        definition only and gets an empty use-site list.
        """
        evidence = state.evidence
        if (
            not self.settings.exact_enabled
            or self.exact_store is None
            or state.scope is None
            or not state.scope.blob_names
            or evidence is None
            or not state.identifiers
        ):
            return [], [], []
        store = self.exact_store
        scope = state.scope
        top_k = self.settings.default_top_k
        identifiers = state.lookup_identifiers or state.identifiers

        async def lookup_one(
            identifier: str, kinds: Sequence[str] | None
        ) -> list[SearchHit]:
            hits = await store.search_exact(
                identifiers=(identifier,), scope=scope, top_k=top_k, kinds=kinds
            )
            # A qualified request can share a query with unrelated bare names
            # (for example ``Session.get`` plus ``Cache``). Filter only the
            # qualified identifier's own batch; applying one scope predicate to
            # the combined result would silently discard the bare name. A
            # declaration batch may use the declaration-line evidence; a batch
            # of use sites or mixed occurrences is pinned by structure or
            # text only.
            declarations = kinds is not None and set(kinds) <= set(DEFINITION_KINDS)
            for leaf, scopes in state.qualifiers.items():
                if identifier == leaf or any(
                    identifier.endswith(f"{separator}{leaf}")
                    for separator in _QUALIFIER_SEPARATORS
                ):
                    return resolve_qualified_hits(
                        hits, {leaf: scopes}, declarations=declarations, strict=True
                    )
            return hits

        async def lookup(
            kinds: Sequence[str] | None, requested: Sequence[str] | None = None
        ) -> list[SearchHit]:
            names = identifiers if requested is None else requested
            if not state.qualifiers:
                return await store.search_exact(
                    identifiers=names, scope=scope, top_k=top_k, kinds=kinds
                )
            batches = await asyncio.gather(
                *(lookup_one(identifier, kinds) for identifier in names)
            )
            merged: list[SearchHit] = []
            seen: set[SearchHitKey] = set()
            for batch in batches:
                for hit in batch:
                    key = search_hit_key(hit)
                    if key in seen:
                        continue
                    seen.add(key)
                    merged.append(hit)
            return merged

        use_sites: list[SearchHit] = []
        try:
            with state.stage("exact"):
                if state.route.intent == QueryIntent.REFERENCE:
                    occurrences, definitions, use_sites = await asyncio.gather(
                        lookup(None),
                        lookup(DEFINITION_KINDS),
                        lookup(
                            USE_SITE_KINDS,
                            split_qualified_identifiers(
                                state.route.targets or state.identifiers
                            )[0],
                        ),
                    )
                elif state.route.intent == QueryIntent.CALL_CHAIN:
                    occurrences, definitions, state.endpoints = await asyncio.gather(
                        lookup((*DEFINITION_KINDS, CALL_KIND)),
                        lookup(DEFINITION_KINDS),
                        resolve_endpoints(
                            store, scope, state.route.targets, state.qualifiers
                        ),
                    )
                elif state.route.intent == QueryIntent.SYMBOL and len(identifiers) > 1:
                    # "Where is ``fromJson(JsonReader, TypeToken)`` defined":
                    # the first name is the symbol asked for, the others pick
                    # its overload. Its declarations lead in that order; the
                    # damping by declaration count must not let a type named
                    # once outrank the overloaded method asked about.
                    first_leaf = _leaf(identifiers[0])
                    *batches, overloads = await asyncio.gather(
                        *(
                            lookup_one(identifier, DEFINITION_KINDS)
                            for identifier in identifiers
                        ),
                        store.find_definitions(
                            identifiers=(first_leaf,),
                            scope=scope,
                            max_per_identifier=40,
                        ),
                    )
                    others = [
                        *state.identifiers[1:],
                        *(
                            scope_name
                            for scopes in state.qualifiers.values()
                            for scope_name in scopes
                        ),
                    ]
                    batches[0] = order_by_signature_comentions(
                        batches[0], overloads, others
                    )
                    state.primary_definition_found = bool(state.route.targets)
                    for target in state.route.targets:
                        target_hits = [
                            hit
                            for identifier, batch in zip(
                                identifiers, batches, strict=True
                            )
                            if _leaf(identifier) == _leaf(target)
                            for hit in batch
                        ]
                        # Two scopes can share a leaf. One scope resolving
                        # must not authorize skipping recall for the other.
                        target_qualifiers = split_qualified_identifiers((target,))[1]
                        if not resolve_qualified_hits(
                            target_hits, target_qualifiers, strict=True
                        ):
                            state.primary_definition_found = False
                            break
                    definitions = []
                    seen_keys: set[SearchHitKey] = set()
                    for batch in batches:
                        for hit in batch:
                            key = search_hit_key(hit)
                            if key not in seen_keys:
                                seen_keys.add(key)
                                definitions.append(hit)
                    occurrences = definitions
                else:
                    definitions = await lookup(DEFINITION_KINDS)
                    occurrences = definitions
                    if state.route.intent == QueryIntent.SYMBOL:
                        state.primary_definition_found = bool(
                            definitions and state.route.targets
                        )
        except Exception as exc:
            logger.warning(
                "Exact identifier recall failed; using semantic candidates: {}", exc
            )
            return [], [], []
        # A qualified request (``Session.get``) pins the leaf to a scope; the
        # filtering was applied to that identifier's batch above. Among the
        # remaining declarations, the ones that mention the request's other
        # names (parameter types of an overload) come first.
        if state.route.intent == QueryIntent.SYMBOL and len(identifiers) == 1:
            # The request's other names: the scopes of a qualified name. The
            # looked-up name itself is in every hit.
            others = [scope for scopes in state.qualifiers.values() for scope in scopes]
            occurrences = order_by_comentions(occurrences, others)
            definitions = order_by_comentions(definitions, others)
        elif state.route.intent == QueryIntent.REFERENCE and len(state.identifiers) > 1:
            # "Where is ``IntoResponse`` implemented for ``StatusCode``": the
            # use site that names the other symbol too is the one asked for.
            others = list(state.identifiers[1:])
            occurrences = order_by_comentions(occurrences, others)
            use_sites = order_by_comentions(use_sites, others)
        if state.audit is not None:
            state.audit.exact_definitions = len(definitions)
            state.audit.definition_sites = len(definitions)
        return occurrences, definitions, use_sites

    async def _recall_anchors(self, state: RetrievalState) -> list[SearchHit]:
        """Definition chunks an issue-style request points at deterministically.

        Two facts in a bug report tie a name to a place: a traceback frame
        names the function together with the file that declares it, and the
        title names the symbol the report is about. Frames come first,
        outermost project frame first (the API the reporter called, then
        the code it delegated to), one per file; then the declarations of
        the title's identifiers, qualified names pinned to their scope and
        only when the name is declared in at most three places. Names
        mentioned only in the body (a minimal example's helpers, fixture
        names, unrelated types) anchor nothing.
        """
        evidence = state.evidence
        if (
            state.route.intent != QueryIntent.COMPOUND
            or self.settings.compound_anchor_slots <= 0
            or not self.settings.exact_enabled
            or self.exact_store is None
            or state.scope is None
            or not state.scope.blob_names
            or evidence is None
            or not (evidence.frames or state.identifiers)
        ):
            return []
        store = self.exact_store
        scope = state.scope
        title = state.query.strip().splitlines()[0] if state.query.strip() else ""
        title_identifiers = [
            identifier
            for identifier in state.lookup_identifiers
            if _word_in(_leaf(identifier), title)
            and _leaf(identifier).lower() not in _IDENTIFIER_NOISE
        ]
        frame_functions = tuple(
            dict.fromkeys(frame.function for frame in evidence.frames)
        )
        try:
            with state.stage("exact"):
                frame_definitions, title_definitions = await asyncio.gather(
                    store.find_definitions(
                        identifiers=frame_functions, scope=scope, max_per_identifier=40
                    )
                    if frame_functions
                    else _no_definitions(),
                    store.find_definitions(
                        identifiers=tuple(title_identifiers),
                        scope=scope,
                        max_per_identifier=3,
                    )
                    if title_identifiers
                    else _no_definitions(),
                )
        except Exception as exc:
            logger.warning("Anchor definition recall failed: {}", exc)
            return []
        anchors: list[SearchHit] = []
        seen: set[SearchHitKey] = set()
        anchored_files: set[str] = set()
        for frame in evidence.frames:
            matching = [
                definition
                for definition in frame_definitions
                if definition.identifier == frame.function
                and definition.hit.blob_name not in anchored_files
                and _frame_matches(frame.path, definition.hit.path)
            ]
            if not matching:
                continue
            # The frame's line picks the declaration among same-named ones
            # in the file (``BaseAdapter.send`` vs ``HTTPAdapter.send``).
            containing = [
                definition
                for definition in matching
                if frame.line is not None
                and definition.start_line <= frame.line <= definition.end_line
            ]
            definition = (containing or matching)[0]
            hit = definition.hit
            anchored_files.add(hit.blob_name)
            key = search_hit_key(hit)
            if key not in seen:
                seen.add(key)
                anchors.append(hit)
        for leaf, scopes in state.qualifiers.items():
            pinned = [item for item in title_definitions if item.identifier == leaf]
            if pinned:
                kept = {
                    search_hit_key(item.hit)
                    for item in resolve_qualified_definitions(
                        pinned, {leaf: scopes}, strict=True
                    )
                }
                title_definitions = [
                    item
                    for item in title_definitions
                    if item.identifier != leaf or search_hit_key(item.hit) in kept
                ]
        for definition in title_definitions:
            key = search_hit_key(definition.hit)
            if key not in seen:
                seen.add(key)
                anchors.append(definition.hit)
        return anchors

    def _can_recall_lexical(self, state: RetrievalState) -> bool:
        evidence = state.evidence
        return bool(
            self.settings.lexical_enabled
            and self.lexical_store is not None
            and state.scope is not None
            and state.scope.blob_names
            and evidence is not None
            and (evidence.terms or evidence.phrases)
        )

    def _should_recall_lexical_eagerly(self, state: RetrievalState) -> bool:
        evidence = state.evidence
        return self._can_recall_lexical(state) and bool(
            state.strategy.enable_lexical_recall
            or (evidence is not None and evidence.phrases)
        )

    def _should_recall_lexical_fallback(self, state: RetrievalState) -> bool:
        if self._should_recall_lexical_eagerly(state):
            return False
        if not self._can_recall_lexical(state):
            return False
        if state.route.intent == QueryIntent.SYMBOL:
            return not state.exact
        if state.route.intent == QueryIntent.PATH:
            return not state.lookup_scores
        return False

    async def _recall_lexical(
        self, state: RetrievalState, *, routed: bool
    ) -> list[SearchHit]:
        evidence = state.evidence
        if not routed or not self._can_recall_lexical(state) or evidence is None:
            return []
        try:
            with state.stage("lexical"):
                async with asyncio.timeout(self.settings.lexical_timeout_seconds):
                    return await self.lexical_store.search_lexical(
                        terms=self._lexical_terms(state),
                        phrases=evidence.phrases,
                        scope=state.scope,
                        top_k=self.settings.lexical_top_k,
                        required=self._lexical_required(state),
                    )
        except TimeoutError:
            logger.warning("Lexical recall timed out; using other candidates")
            return []
        except Exception as exc:
            logger.warning("Lexical recall failed: {}", type(exc).__name__)
            return []

    @staticmethod
    def _lexical_terms(state: RetrievalState) -> tuple[str, ...]:
        evidence = state.evidence
        assert evidence is not None
        if state.route.intent != QueryIntent.SYMBOL or not state.identifiers:
            return evidence.terms

        # Exact lookup already tried the identifier itself. Its lexical fallback
        # should use the joined surrogate (TargetService -> targetservice), not
        # broad sub-words such as target/service that dominate large term indexes.
        # Qualified identifiers use separate index tokens, so retain each part.
        terms: list[str] = []
        for identifier in state.identifiers:
            tokens = lexical_tokens(identifier)
            single_lexeme = identifier.replace("_", "").isalnum()
            selected = (
                (max(tokens, key=len),) if single_lexeme and tokens else tuple(tokens)
            )
            for token in selected:
                if token and token not in terms:
                    terms.append(token)
        return tuple(terms) or evidence.terms

    @staticmethod
    def _lexical_required(state: RetrievalState) -> tuple[str, ...]:
        """Whole-identifier tokens a use-site lookup must contain.

        The exact lane only knows declarations and imports, so lexical recall
        is the one operator that reaches call sites. Ranking ``get OR json``
        by BM25 favours chunks dense in ``json``; gating on the joined
        surrogate ``getjson`` keeps the lane on chunks that name the symbol.
        Every whole identifier is a valid answer, so several gate as OR.
        Call-chain questions stay ungated: their far end is described in
        words ("its base service") and rarely repeats the named symbol.
        """
        evidence = state.evidence
        if state.route.intent != QueryIntent.REFERENCE or evidence is None:
            return ()
        required: list[str] = []
        for identifier in state.route.targets or state.identifiers:
            leaf = identifier.rsplit("::", 1)[-1].rsplit(".", 1)[-1]
            tokens = lexical_tokens(leaf)
            if not tokens:
                continue
            surrogate = max(tokens, key=len)
            if surrogate not in required:
                required.append(surrogate)
        return tuple(required)

    async def _recall_paths(self, state: RetrievalState) -> dict[str, float]:
        """Best path score per blob over every query variant; failures degrade to none."""
        if (
            not state.use_path_index
            or self.path_store is None
            or state.embed_error is not None
        ):
            return {}
        allowed = state.allowed_blob_names
        blob_filter = list(allowed) if allowed else None
        path_scores: dict[str, float] = {}
        try:
            with state.vector_stage("path"):
                result_lists = await asyncio.gather(
                    *(
                        self.path_store.search_paths(
                            query_vector=state.vectors[variant],
                            allowed_blob_names=blob_filter,
                            top_k=self.settings.path_top_k,
                        )
                        for variant in state.path_queries
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

    async def _recall_path_lookup(self, state: RetrievalState) -> dict[str, float]:
        """Exact path/basename evidence (explicit filenames, traceback frames)."""
        evidence = state.evidence
        if (
            not self.settings.path_lookup_enabled
            or self.path_lookup_store is None
            or state.scope is None
            or not state.scope.blob_names
            or evidence is None
            or not evidence.has_path_evidence
        ):
            return {}
        try:
            with state.stage("path_lookup"):
                return await self.path_lookup_store.match_paths(
                    filenames=evidence.filenames,
                    paths=evidence.paths,
                    scope=state.scope,
                    limit=self.settings.path_top_k,
                )
        except Exception as exc:
            logger.warning("Path lookup failed: {}", type(exc).__name__)
            return {}

    # ── fuse ─────────────────────────────────────────────────────────────

    async def _fuse(self, state: RetrievalState) -> None:
        with state.stage("fuse"):
            hits = state.dense
            if not hits and state.dense_route and state.dense_route.startswith("skip:"):
                # Vector recall was not awaited: the exact lane is the primary
                # list, and lexical evidence joins it by rank so its raw BM25
                # scores never order the candidates on their own.
                hits = list(state.exact)
            if state.route.intent == QueryIntent.COMPOUND and state.exact and hits:
                # An issue names many identifiers (its example's helpers,
                # every traceback frame, the types it mentions); their
                # declarations are one more ranked list, not a score that
                # outbids the fused order. The deterministic ones become
                # anchors and take the head below.
                hits = self._fuse_lists([hits], state.lexical, extra=[state.exact])
            elif state.lexical:
                hits = self._fuse_lists(
                    [hits] if hits else [],
                    state.lexical,
                )
            hits = self._merge_exact_hits(state.route.intent, state.exact, hits)
            if (
                state.route.intent == QueryIntent.CALL_CHAIN
                and len(state.endpoints) >= 2
            ):
                present = {search_hit_key(hit) for hit in hits}
                for _endpoint, definitions in state.endpoints[:2]:
                    first = definitions[0].hit
                    if search_hit_key(first) not in present:
                        hits.append(first)
                        present.add(search_hit_key(first))
            structural = state.anchors
            if structural:
                # Anchored definitions must be in the window the head
                # rules order; their own recall score is not comparable to
                # RRF, so they are appended and promoted by key.
                present = {search_hit_key(hit) for hit in hits}
                hits = [
                    *hits,
                    *(hit for hit in structural if search_hit_key(hit) not in present),
                ]
            boosts = dict(state.lookup_scores)
            for blob_name, score in state.path_scores.items():
                boosts[blob_name] = max(score, boosts.get(blob_name, float("-inf")))
            if boosts:
                # Embedding path hits are the only answer to a pure filename
                # query, so files the content index missed are backfilled.
                # Lookup hits from traceback frames only boost: their first
                # chunk would be imports, not the failing code. A path request
                # is the exception: its SQL match is the answer and must own a
                # chunk even when the content index never mentions the name.
                backfill = set(state.path_scores)
                if (
                    state.use_path_index
                    or not hits
                    or state.route.intent == QueryIntent.PATH
                ):
                    backfill |= set(state.lookup_scores)
                hits = await self._merge_path_and_content(boosts, hits, backfill)
            elif state.use_path_index:
                logger.info("No path results, using content-only")
            hits = self._filter_qualified_candidates(state, hits)
            if state.route.intent == QueryIntent.REFERENCE and state.route.targets:
                # Dense can recover uses missed by SQL, but semantic similarity
                # alone does not establish a reference to the requested name.
                # Apply the same whole-name evidence requirement as lexical
                # recall; exact rows already carry scoped occurrence evidence.
                occurrences = {search_hit_key(hit) for hit in state.exact}
                hits = [
                    hit
                    for hit in hits
                    if search_hit_key(hit) in occurrences
                    or any(
                        mentions_requested_name(hit, name)
                        for name in state.route.targets
                    )
                ]
            # 路径索引失败且内容检索也失败：没有任何候选时才把内容错误抛出。
            if not hits and state.dense_error is not None:
                raise state.dense_error
            state.candidates = hits

    @staticmethod
    def _filter_qualified_candidates(
        state: RetrievalState, hits: list[SearchHit]
    ) -> list[SearchHit]:
        """Keep a qualified symbol query inside its proven scope.

        Exact recall applies this rule per identifier, but dense and lexical
        candidates can still introduce an unrelated bare-name definition. Only
        filter symbol queries, and only when a candidate actually proves the
        requested qualifier; an unknown qualifier keeps the normal fallback.
        """
        if state.route.intent != QueryIntent.SYMBOL or not state.qualifiers or not hits:
            return hits
        # "Where are ``Session.get`` and ``Cache`` defined": the scope pins
        # ``get`` only; a bare name asked for alongside keeps its candidates.
        qualified = {leaf for leaf in state.qualifiers} | {
            identifier
            for identifier in state.lookup_identifiers
            if any(sep in identifier for sep in _QUALIFIER_SEPARATORS)
        }
        if any(identifier not in qualified for identifier in state.lookup_identifiers):
            return hits
        resolved = resolve_qualified_hits(hits, state.qualifiers)
        if len(resolved) < len(hits):
            return resolved
        return hits

    def _fuse_lists(
        self,
        dense_lists: list[list[SearchHit]],
        lexical: list[SearchHit] | None = None,
        *,
        extra: Sequence[list[SearchHit]] = (),
    ) -> list[SearchHit]:
        """Weighted reciprocal rank fusion over dense facet lists plus lexical.

        Raw scores never mix: dense cosine, BM25/ts_rank and RRF do not share a
        scale, so every list contributes by rank only. A single dense list with
        no lexical companion retains its original ranking.
        """
        lists = list(dense_lists)
        weights = (
            [1.0] + [self.settings.query_facet_weight] * (len(lists) - 1)
            if lists
            else []
        )
        if lexical:
            lists.append(lexical)
            weights.append(self.settings.lexical_weight)
        # Structural lists (declarations of the request's identifiers) go
        # first so that, at equal fused score, deterministic evidence leads.
        for ranked in reversed([ranked for ranked in extra if ranked]):
            lists.insert(0, ranked)
            weights.insert(0, 1.0)
        if not lists:
            return []
        if len(lists) == 1:
            return lists[0]

        rrf_k = self.settings.rrf_k
        max_score = sum(weight / (rrf_k + 1) for weight in weights)
        scores: dict[SearchHitKey, float] = {}
        hits_by_key: dict[SearchHitKey, SearchHit] = {}
        first_seen: dict[SearchHitKey, int] = {}
        ordinal = 0
        for weight, hits in zip(weights, lists, strict=True):
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

    def _merge_exact_hits(
        self,
        intent: QueryIntent,
        exact_hits: list[SearchHit],
        semantic_hits: list[SearchHit],
    ) -> list[SearchHit]:
        if intent == QueryIntent.SYMBOL and exact_hits:
            # A definition lookup is a structural lane, not another score to
            # calibrate against cosine/RRF. Keep its leading answer in the
            # candidate window; _rank restores that deterministic head after
            # source priors, while still leaving room for semantic context.
            merged: list[SearchHit] = []
            seen: set[SearchHitKey] = set()
            for hit in [*exact_hits, *semantic_hits]:
                key = search_hit_key(hit)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(hit)
            return merged[: self.settings.default_top_k]

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

        if intent == QueryIntent.COMPOUND and semantic_hits:
            # Already fused by rank in ``_fuse``; declarations the fusion
            # window dropped follow the fused order instead of outbidding it.
            present = {search_hit_key(hit) for hit in semantic_hits}
            return [
                *semantic_hits,
                *(hit for hit in exact_hits if search_hit_key(hit) not in present),
            ][: self.settings.default_top_k]

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

    async def _merge_path_and_content(
        self,
        path_scores: dict[str, float],
        content_hits: list[SearchHit],
        backfill: set[str],
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
        missing = [
            name for name in path_scores if name in backfill and name not in covered
        ]
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

    # ── prior + rerank ───────────────────────────────────────────────────

    async def _rank(self, state: RetrievalState) -> None:
        """Apply static priors, then one candidate-preserving rerank state machine."""
        # 路径类查询使用文档中立先验（不降权 .rst/.md/.txt）；明确问测试的短查询
        # 同样中立，否则被问到的测试文件反而会被压后。issue 文本只是顺带提到
        # 文件名或测试失败，仍然是在找源码，先验照常生效。
        priority_factor = (
            neutral_priority_factor
            if state.strategy.enable_path_index
            or (
                state.route.intent != QueryIntent.COMPOUND
                and state.route.tests_requested
            )
            else self.priority_factor
        )
        boosted = self._working_set(state.scope)

        # Static source priors prepare the candidate order. Model rerankers run
        # afterwards, so their returned order cannot be silently overwritten.
        hits = apply_priors(
            state.candidates,
            priority_factor=priority_factor,
            boosted=boosted,
            working_set_boost=self.settings.working_set_boost,
        )
        headers = await self._header_chunks(state, hits)
        state.implementations = await self._implementation_occurrences(state)
        implementors = frozenset(
            search_hit_key(item.hit) for item in state.implementations
        )
        head_evidence = HeadEvidence(
            route=state.route,
            identifiers=state.identifiers,
            qualifiers=state.qualifiers,
            definitions=state.definitions,
            exact=state.exact,
            use_sites=state.use_sites,
            lexical=state.lexical,
            header_keys=headers,
            implementor_keys=implementors,
        )
        source_slots = source_heads(
            head_evidence,
            hits,
            priority_factor,
            slots=min(3, self.settings.final_select_k)
            if state.route.intent == QueryIntent.REFERENCE
            else self.settings.source_head_slots,
            reference_fallback=self.settings.reference_head_fallback,
        )
        protected = structural_heads(
            hits,
            intent=state.route.intent,
            exact=state.exact,
            anchors=state.anchors,
            endpoints=state.endpoints,
            lookup_scores=state.lookup_scores,
            settings=self.settings,
            priority_factor=priority_factor,
        )
        if state.route.intent == QueryIntent.REFERENCE:
            protected = source_slots
        if state.audit is not None:
            state.audit.head_slots = len(protected)
        hits = promote_heads(hits, tuple(dict.fromkeys((*protected, *source_slots))))

        decision = plan_rerank(
            state.route.intent,
            len(hits),
            has_exact_hits=state.primary_definition_found,
            # Embedding path similarity is useful recall but not deterministic
            # evidence. Only an exact SQL path/basename match may skip reranking.
            has_path_hits=bool(state.lookup_scores),
            dedicated_enabled=self.reranker is not None,
            llm_enabled=self.llm_reranker is not None,
            dedicated_policy=self.settings.rerank_policy,
            llm_policy=self.settings.llm_rerank_policy,
        )
        state.decision = decision
        if state.audit is not None:
            state.audit.rerank_route = decision.route
        if decision.dedicated and self.reranker is not None:
            with state.stage("rerank"):
                hits = await self.reranker.rerank(state.query, hits)
        if decision.llm and self.llm_reranker is not None:
            with state.stage("llm_rerank"):
                hits = await self.llm_reranker.rerank(state.query, hits)
        # Seed the model window above, then restore the same structural keys.
        # Source priors do not overwrite a model's final semantic ordering.
        state.candidates = promote_heads(hits, protected)

    async def _header_chunks(
        self, state: RetrievalState, hits: Sequence[SearchHit]
    ) -> frozenset[tuple[str, str]]:
        """Record which candidates are import-only file headers (one SQL lookup).

        A chunk whose recorded symbol evidence is imports and nothing else is
        the top of a file: ``use``/``import`` lines, a license comment, a
        module docstring. It names every module the file touches, which is
        why it sits close to architecture and flow questions in vector space,
        and it implements none of them. Chunks with no evidence at all are
        left alone: a script body or a config block may be the answer.
        """
        if (
            not self.settings.head_skips_import_headers
            or self.exact_store is None
            or state.scope is None
            or state.route.intent in (QueryIntent.SYMBOL, QueryIntent.PATH)
        ):
            return frozenset()
        occurrences = tuple(
            dict.fromkeys(
                (hit.blob_name, hit.content_hash)
                for hit in hits
                if hit.blob_name and hit.content_hash
            )
        )
        if not occurrences:
            return frozenset()
        try:
            kinds = await self.exact_store.occurrence_kinds(occurrences, state.scope)
        except Exception as exc:
            logger.warning("Occurrence lookup for head slots failed: {}", exc)
            return frozenset()
        header_kinds = frozenset(HEADER_KINDS)
        return frozenset(
            key for key, seen in kinds.items() if seen and seen <= header_kinds
        )

    async def _implementation_occurrences(
        self, state: RetrievalState
    ) -> list[RelatedOccurrence]:
        """Bounded inherit facts for the requested interface and optional subtype.

        "Where is ``IntoResponse`` implemented for ``StatusCode``": the impl
        block is the inherit occurrence of the trait whose enclosing type is
        the other named symbol, an exact index fact rather than a co-mention.
        """
        store = self.relation_store
        if (
            store is None
            or state.scope is None
            or state.route.intent != QueryIntent.REFERENCE
            or not state.route.implementations_requested
            or not state.route.targets
        ):
            return []
        try:
            implementations = await store.find_implementations(
                identifiers=tuple(_leaf(name) for name in state.route.targets),
                scope=state.scope,
                limit=200,
            )
        except Exception as exc:
            logger.warning("Implementor lookup failed: {}", type(exc).__name__)
            return []
        if state.qualifiers:
            scoped = {
                search_hit_key(hit)
                for hit in resolve_qualified_hits(
                    [item.hit for item in implementations],
                    state.qualifiers,
                    declarations=False,
                    strict=True,
                )
            }
            implementations = [
                item for item in implementations if search_hit_key(item.hit) in scoped
            ]
        wanted = {
            _leaf(name) for name in state.identifiers if name not in state.route.targets
        }
        return [
            item for item in implementations if not wanted or item.enclosing in wanted
        ]

    def _working_set(self, scope: SearchScope | None) -> frozenset[str]:
        """Blobs the request just added: the files the user is editing right now.

        A first full sync adds everything, which carries no information, so
        the prior only applies below the configured delta size.
        """
        if scope is None or not scope.added_blob_names:
            return frozenset()
        limit = self.settings.working_set_boost_max_blobs
        if limit <= 0 or len(scope.added_blob_names) > limit:
            return frozenset()
        if self.settings.working_set_boost <= 1.0:
            return frozenset()
        return scope.added_blob_names

    # ── select ───────────────────────────────────────────────────────────

    async def _select(self, state: RetrievalState) -> None:
        with state.stage("select"):
            state.selected = await self.selector.select(
                state.candidates,
                self.settings.final_select_k,
                mode=state.strategy.selection_mode,
                max_chars=self._context_budget(state),
            )

    def _context_budget(self, state: RetrievalState) -> int:
        if state.strategy.selection_mode == SelectionMode.FOCUSED:
            return self.settings.focused_max_context_chars
        return self.settings.max_context_chars

    def _expands_relations(self, state: RetrievalState) -> bool:
        strategy = state.strategy
        if state.scope is None or not state.scope.blob_names:
            return False
        outbound = (
            self.settings.related_definitions_enabled
            and strategy.expand_related_definitions
            and self.exact_store is not None
        )
        return (
            outbound or bool(self._relation_lanes(state)) or self._traces_chain(state)
        )

    def _traces_chain(self, state: RetrievalState) -> bool:
        return (
            state.route.intent == QueryIntent.CALL_CHAIN
            and len(state.endpoints) >= 1
            and self.exact_store is not None
        )

    # ── expand ───────────────────────────────────────────────────────────

    async def _expand(self, state: RetrievalState) -> None:
        if self.settings.merge_adjacent_enabled:
            state.selected = merge_adjacent_hits(state.selected)
        if not state.selected or not self._expands_relations(state):
            return
        assert state.scope is not None
        settings = self.settings
        with state.stage("expand"):
            lanes = self._relation_lanes(state)
            relation_cap = self._relation_budget_cap(state, lanes)
            remaining = self._context_budget(state) - sum(
                len(hit.content) for hit in state.selected
            )
            chain: list[SearchHit] = []
            if self._traces_chain(state):
                # A chain may displace the primary tail, but the leading
                # answer remains. Bound the chain before rendering so even an
                # operator-supplied chain cap larger than the context cannot
                # break the hard response budget.
                chain_budget = max(
                    0,
                    self._context_budget(state) - len(state.selected[0].content),
                )
                if chain_budget > 0:
                    chain = (
                        await trace_path(
                            self.exact_store,
                            state.scope,
                            state.endpoints,
                            state.selected,
                            settings,
                            max_chars=chain_budget,
                        )
                        if len(state.endpoints) >= 2
                        else await trace_callees(
                            self.exact_store,
                            state.scope,
                            state.endpoints,
                            state.selected,
                            settings,
                            max_chars=chain_budget,
                        )
                    )
                if chain:
                    self._make_relation_room(
                        state, sum(len(hit.content) for hit in chain)
                    )
                    remaining = self._context_budget(state) - sum(
                        len(hit.content) for hit in (*state.selected, *chain)
                    )
            identifiers = state.lookup_identifiers
            if lanes and not identifiers:
                # A feature request names no symbol; the symbols its top
                # results declare are what its tests exercise.
                identifiers = await self._selected_definition_names(state)
            wants_related = (
                settings.related_definitions_enabled
                and state.strategy.expand_related_definitions
                and self.exact_store is not None
            )
            lane_tasks = [
                lane.fetch(identifiers) if identifiers else _no_occurrences()
                for lane in lanes
            ]
            results = await asyncio.gather(
                (
                    self._definition_candidates(
                        state, budget=max(remaining, relation_cap)
                    )
                    if wants_related
                    else _no_definition_candidates()
                ),
                *lane_tasks,
                return_exceptions=True,
            )
            candidates = (
                results[0]
                if isinstance(results[0], DefinitionCandidates)
                else DefinitionCandidates()
            )
            related = select_related_definitions(
                candidates,
                state.selected,
                max_chars=min(settings.related_max_chars, max(remaining, relation_cap)),
                max_symbols=settings.related_max_symbols,
                snippet_lines=settings.related_snippet_lines,
            )
            sections: list[SectionInput] = []
            for lane, result in zip(lanes, results[1:], strict=True):
                if isinstance(result, BaseException):
                    logger.warning(
                        "{} lookup failed: {}", lane.role, type(result).__name__
                    )
                    continue
                sections.append(
                    SectionInput(lane.role, result, lane.max_items, lane.max_chars)
                )
            if isinstance(results[0], BaseException):
                logger.warning(
                    "Related definition lookup failed: {}", type(results[0]).__name__
                )
            related_first = state.route.intent not in (
                QueryIntent.SYMBOL,
                QueryIntent.REFERENCE,
            )
            shown = [*state.selected, *chain]
            preview = assemble_sections(
                selected=shown,
                related=related,
                sections=sections,
                remaining_chars=relation_cap if relation_cap > 0 else max(remaining, 0),
                snippet_lines=settings.relation_snippet_lines,
                related_first=related_first,
            )
            if preview.hits and preview.chars > remaining:
                chain_chars = sum(len(hit.content) for hit in chain)
                self._make_relation_room(
                    state, chain_chars + min(preview.chars, relation_cap)
                )
                remaining = self._context_budget(state) - sum(
                    len(hit.content) for hit in (*state.selected, *chain)
                )
                shown = [*state.selected, *chain]
                related = select_related_definitions(
                    candidates,
                    state.selected,
                    max_chars=min(settings.related_max_chars, remaining, relation_cap),
                    max_symbols=settings.related_max_symbols,
                    snippet_lines=settings.related_snippet_lines,
                )
            pack = assemble_sections(
                selected=shown,
                related=related,
                sections=sections,
                remaining_chars=(
                    min(remaining, relation_cap) if relation_cap > 0 else remaining
                ),
                snippet_lines=settings.relation_snippet_lines,
                related_first=related_first,
            )
            state.related = [*chain, *pack.hits]
            if state.audit is not None:
                counts = dict(pack.counts)
                if chain:
                    counts["chain"] = len(chain)
                state.audit.relation_counts = counts
                state.audit.relation_chars = pack.chars + sum(
                    len(hit.content) for hit in chain
                )

    def _relation_budget_cap(
        self, state: RetrievalState, lanes: Sequence[_RelationLane]
    ) -> int:
        """Bound relation spend by active lane caps and the context scale.

        The configured value remains an upper bound, not an unconditional
        reservation. A quarter of the active context keeps answers readable
        across focused and broad selection budgets without tying the policy to
        one benchmark's chunk sizes.
        """
        active = sum(lane.max_chars for lane in lanes)
        if state.strategy.expand_related_definitions and self.exact_store is not None:
            active += self.settings.related_max_chars
        if active <= 0:
            return 0
        context = self._context_budget(state)
        return min(
            self.settings.relation_reserve_chars,
            active,
            max(_MIN_RELATED_BUDGET, context // 4),
        )

    def _make_relation_room(self, state: RetrievalState, target: int) -> None:
        """Trim only the lowest-priority primary tail when evidence exists."""
        if target <= 0:
            return
        budget = self._context_budget(state)
        kept = list(state.selected)
        while kept and sum(len(hit.content) for hit in kept) + target > budget:
            kept.pop()
        if kept:
            state.selected = kept

    def _relation_lanes(self, state: RetrievalState) -> list[_RelationLane]:
        """Relation sections the intent asks for, in fill order.

        Re-exports are tiny and pin the public import path, so they are filled
        first; tests come last because a test file is the largest excerpt and
        the least specific to the exact question.
        """
        store = self.relation_store
        if store is None or state.scope is None or not state.scope.blob_names:
            return []
        scope = state.scope
        settings = self.settings
        strategy = state.strategy
        lanes: list[_RelationLane] = []
        if strategy.expand_reexports and settings.reexports_enabled:
            lanes.append(
                _RelationLane(
                    "reexport",
                    lambda names: store.find_reexports(
                        identifiers=names, scope=scope, limit=settings.reexports_max
                    ),
                    settings.reexports_max,
                    settings.reexports_max_chars,
                )
            )
        if strategy.expand_callers and settings.callers_enabled:
            if state.route.intent == QueryIntent.CALL_CHAIN:

                async def fetch_callers(
                    names: Sequence[str],
                ) -> list[RelatedOccurrence]:
                    return await trace_callers(
                        store,
                        self.exact_store,
                        names,
                        scope,
                        max_hops=settings.call_chain_max_hops,
                        max_callers=settings.callers_max,
                    )
            else:

                async def fetch_callers(
                    names: Sequence[str],
                ) -> list[RelatedOccurrence]:
                    return await store.find_callers(
                        identifiers=names,
                        scope=scope,
                        limit=settings.callers_max * 2,
                    )

            lanes.append(
                _RelationLane(
                    "caller",
                    fetch_callers,
                    settings.callers_max,
                    settings.callers_max_chars,
                )
            )
        if (
            strategy.expand_implementations or state.route.implementations_requested
        ) and settings.implementations_enabled:

            async def fetch_implementations(
                names: Sequence[str],
            ) -> list[RelatedOccurrence]:
                if state.route.implementations_requested:
                    return state.implementations
                return await store.find_implementations(
                    identifiers=names,
                    scope=scope,
                    limit=settings.implementations_max * 2,
                )

            lanes.append(
                _RelationLane(
                    "implementation",
                    fetch_implementations,
                    settings.implementations_max,
                    settings.implementations_max_chars,
                )
            )
        if strategy.expand_tests and settings.tests_enabled:
            # "Where is X defined" wants the declaration; one test shows how
            # it is exercised. Requests that ask for tests keep the full slot.
            tests_max = (
                1 if state.route.intent == QueryIntent.SYMBOL else settings.tests_max
            )
            lanes.append(
                _RelationLane(
                    "test",
                    lambda names: store.find_test_uses(
                        identifiers=names, scope=scope, limit=tests_max * 2
                    ),
                    tests_max,
                    settings.tests_max_chars,
                )
            )
        return lanes

    async def _selected_definition_names(
        self, state: RetrievalState
    ) -> tuple[str, ...]:
        store = self.relation_store
        if store is None or state.scope is None:
            return ()
        sources = state.selected[: self.settings.related_source_hits]
        pairs = [
            (hit.blob_name, hit.content_hash)
            for hit in sources
            if hit.blob_name and hit.content_hash
        ]
        if not pairs:
            return ()
        try:
            defined = await store.defined_identifiers(pairs, state.scope)
        except Exception as exc:
            logger.warning("Defined-name lookup failed: {}", type(exc).__name__)
            return ()
        names: list[str] = []
        for pair in pairs:
            for name in defined.get(pair, ()):
                if name not in names:
                    names.append(name)
        return tuple(names[: self.settings.related_max_symbols])

    async def _declarations_of(
        self, state: RetrievalState, identifiers: Sequence[str]
    ) -> list[DefinitionHit]:
        """The resolved declarations of a reference request's identifiers."""
        assert self.exact_store is not None and state.scope is not None
        rows = await self.exact_store.find_definitions(
            identifiers=identifiers, scope=state.scope, max_per_identifier=40
        )
        declared = _declaration_keys(state.definitions)
        return [item for item in rows if search_hit_key(item.hit) in declared]

    async def _definition_candidates(
        self, state: RetrievalState, *, budget: int | None = None
    ) -> DefinitionCandidates:
        """Signature-sized excerpts of symbols the selected code refers to.

        Query identifiers come first: when the request names a symbol whose
        definition the selection missed, that is the most useful pull. Mined
        identifiers follow, ordered by how many selected hits share them.
        """
        settings = self.settings
        assert self.exact_store is not None and state.scope is not None
        remaining_chars = self._context_budget(state) - sum(
            len(hit.content) for hit in state.selected
        )
        # A tiny tail budget produces fragmented signatures that cost another
        # SQL lookup without explaining a relationship. Keep expansion useful
        # and predictable instead of filling every last character.
        available = max(remaining_chars, budget or 0)
        if available < _MIN_RELATED_BUDGET:
            return DefinitionCandidates()
        sources = state.selected[: settings.related_source_hits]
        named_reference = state.route.intent == QueryIntent.REFERENCE and bool(
            state.route.targets
        )
        # Names the selected code calls are a stronger relation than names it
        # merely mentions (types in annotations, words in docstrings), so
        # they are pulled first.
        called: list[str] = []
        calls_by_source: dict[SearchHitKey, tuple[str, ...]] = {}
        if not named_reference:
            try:
                call_lists = await asyncio.gather(
                    *(
                        self.exact_store.calls_within(
                            blob_name=hit.blob_name,
                            start_line=hit.start_line,
                            end_line=hit.end_line,
                            scope=state.scope,
                        )
                        for hit in sources
                    )
                )
            except Exception as exc:
                logger.warning("Call lookup failed: {}", type(exc).__name__)
                call_lists = []
            for hit, calls in zip(sources, call_lists, strict=False):
                names = tuple(
                    dict.fromkeys(
                        name
                        for name, _line, _enclosing in calls
                        if name.lower() not in _IDENTIFIER_NOISE
                    )
                )
                calls_by_source[search_hit_key(hit)] = names
                called.extend(name for name in names if name not in called)
        ordered: list[str] = []
        # "Where is X used" asks about X: the excerpt worth appending is X's
        # own declaration when the use sites crowded it out, not the
        # definitions of whatever else those use sites happen to call.
        mined: tuple[str, ...] = (
            () if named_reference else (*called, *_mine_identifiers(sources))
        )
        for identifier in (*state.lookup_identifiers, *mined):
            if identifier not in ordered:
                ordered.append(identifier)
        # Symbols defined by the selected code itself need no pull-in; the
        # store returns their chunk so the filter below drops them.
        candidates = ordered[: settings.related_max_symbols * 5]
        if not candidates:
            return DefinitionCandidates()

        if named_reference and state.definitions:
            # The exact lane already resolved the declaration, qualifier
            # included; ``render`` declared in five files would otherwise
            # exceed the ambiguity bound and the answer's own declaration
            # would never be appended.
            definitions = await self._declarations_of(state, candidates)
        else:
            definitions = await self.exact_store.find_definitions(
                identifiers=candidates,
                scope=state.scope,
                max_per_identifier=settings.related_max_definitions_per_symbol,
            )
        # A qualified name pins its leaf to a scope for the related pull exactly
        # as it does for the primary lane: "where is ``Flask.make_response``
        # defined" must not append ``helpers.make_response``, a different
        # function that happens to share the leaf, as the first thing after
        # the answer.
        for leaf, scopes in state.qualifiers.items():
            pinned = [item for item in definitions if item.identifier == leaf]
            if not pinned:
                continue
            kept = {
                search_hit_key(hit)
                for hit in resolve_qualified_hits(
                    [item.hit for item in pinned], {leaf: scopes}, strict=True
                )
            }
            definitions = [
                item
                for item in definitions
                if item.identifier != leaf or search_hit_key(item.hit) in kept
            ]
        return DefinitionCandidates(
            query_names=state.lookup_identifiers,
            source_hits=tuple(sources) if not named_reference else (),
            calls_by_source=calls_by_source,
            names=tuple(candidates),
            definitions=tuple(definitions),
        )


def _declaration_keys(hits: Sequence[SearchHit]) -> set[SearchHitKey]:
    return {search_hit_key(hit) for hit in hits}


async def _no_definition_candidates() -> DefinitionCandidates:
    return DefinitionCandidates()
