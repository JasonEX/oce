"""RetrievalPipeline 领域服务 - 检索状态机

一次检索是一条固定顺序的状态转移，每个阶段只读写 ``RetrievalState`` 上属于它的字段：

    route   查询 → intent / strategy / QueryEvidence（标识符、文件名、路径、报错短语、词元）
    plan    可选 LLM 改写 + 句子级 facet 分解 + 查询向量
    recall  dense | exact | 按意图 lexical | path（embedding 路径索引 + SQL 精确路径查找），并行
    fuse    dense/lexical 按 RRF 融合 → 合并 exact → 路径 boost/回填
    prior   源码先验 × 工作集增量先验 → 保护确定性头部 → 可选置信度门槛
    rerank  plan_rerank 决策 → 专用 reranker → chat-LLM reranker（均保留候选集）
    select  focused / coverage 选择（数量软上限、字符硬预算）
    expand  同文件相邻片段合并 → 按意图与剩余预算附带关系小节：被引用定义、
            调用方、实现/子类、覆盖测试、转出入口（各自独立槽位与字符上限）

rerank 解决「单篇多相关」，select 解决「这一组够全且不冗余」，expand 解决「拿到的
片段引用了什么」。关闭对应开关时每个阶段都退化为恒等变换。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Collection, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from loguru import logger

from oce.domain.chunk.lang import detect_language
from oce.domain.services.embedder import Embedder
from oce.domain.services.evidence_pack import SectionInput, assemble_sections
from oce.domain.services.lexical import lexical_tokens
from oce.domain.services.path_search import PathContentStore, PathSearchStore
from oce.domain.services.query_classifier import (
    QueryIntent,
    asks_about_tests,
    classify_query_intent,
    should_use_path_index,
)
from oce.domain.services.query_evidence import QueryEvidence, extract_query_evidence
from oce.domain.services.query_planner import HeuristicQueryPlanner, QueryPlanner
from oce.domain.services.relations import RelatedOccurrence, RelationStore
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
from oce.domain.services.symbols import CALL_KIND, DEFINITION_KINDS, HEADER_KINDS
from oce.domain.services.test_paths import is_test_path
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
    # 仓库根目录的主 README 显式不降权；子目录 README 是普通文档，多语言 README 降权
    if name in _PRIMARY_README_NAMES:
        return 1.0 if "/" not in p else 0.5
    if stem.startswith("readme"):
        return 0.2
    # 文档目录 / 文档扩展 / 变更记录：说明代码，不是代码本身
    if (
        any(f"/{part}/" in f"/{p}" for part in _DOCUMENT_DIRECTORIES)
        or p.endswith((".md", ".rst", ".txt"))
        or ("/" not in p and "." not in name and stem in _DOCUMENT_STEMS)
    ):
        return 0.5
    if is_test_path(p):
        return 0.6
    # 配置文件和类型桩：需要它们的查询会写出文件名（PATH 意图，中立先验），
    # 其余查询在找实现，这些文件只是碰巧提到同样的名字。
    if name.endswith(_CONFIG_SUFFIXES) or _RC_FILE.match(name) or name.endswith(".pyi"):
        return 0.7
    if name in {"index.ts", "index.tsx", "index.js", "index.jsx", "types.ts"}:
        return 0.85
    if name == "__init__.py":
        return 0.85
    # Editor integrations, shell glue and other files in a language the index
    # does not parse (an Emacs mode next to a Python linter) are real code but
    # rarely what a request about the project is looking for.
    if "." in name and detect_language(name) is None:
        return 0.85
    return 1.0


_DOCUMENT_DIRECTORIES = frozenset(
    {
        "docs",
        "doc",
        "examples",
        "example",
        "samples",
        "sample",
        "changelog",
        "changelogs",
        "news",
    }
)
_DOCUMENT_STEMS = frozenset(
    {"changelog", "changes", "history", "news", "authors", "contributors", "todo"}
)
_PRIMARY_README_NAMES = frozenset(
    {"readme", "readme.md", "readme.rst", "readme.txt", "readme.adoc"}
)
_CONFIG_SUFFIXES = (".cfg", ".ini", ".toml", ".yaml", ".yml", ".json")
# .coveragerc, .pylintrc, pylintrc, tox.ini-style rc files
_RC_FILE = re.compile(r"^\.?[a-z0-9_-]+rc$")


def _is_root_readme(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    name = normalized.rsplit("/", 1)[-1]
    return "/" not in normalized and name.split(".", 1)[0] == "readme"


def neutral_priority_factor(_path: str) -> float:
    """文档中立的优先级因子：恒为 1.0。

    用于关闭 source priority，以及「XX 文件在哪里」类路径查询：任何文件类型都可能
    是目标（文档、配置、测试、license 都在找文件语义内），「源码优先」先验不成立，
    排序完全交由路径 boost + 内容分数 + rerank 决定。
    """
    return 1.0


# Identifiers worth pulling a definition for: multi-part or reasonably long
# names. Short lowercase words are mostly keywords, locals, or English.
_MINED_IDENTIFIER = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b")
_IDENTIFIER_NOISE = frozenset(
    """
    self cls this super none true false null nil return import from export def
    class fn func function const let var static public private protected async
    await yield lambda pass break continue while for foreach else elif if try
    catch except finally raise throw throws new delete typeof instanceof void
    int str float bool list dict set tuple object string number boolean array
    print len range enumerate zip map filter isinstance hasattr getattr setattr
    type super value values key keys item items name args kwargs result data
    error exception warning test tests assert with match case default switch
    struct enum trait impl mod use pub crate where type interface extends
    implements package namespace using module require console
    """.split()
)

_MIN_RELATED_BUDGET = 1_000


def _mine_identifiers(hits: Sequence[SearchHit]) -> list[str]:
    """Identifiers referenced by the hits, most widely shared first.

    cAST may place an enclosing class or function signature in ``context``
    rather than repeating it in a split method chunk. Treat that structural
    header as part of the hit for relation expansion, otherwise base classes
    disappear precisely when chunking is most accurate.
    """
    per_hit: dict[str, set[int]] = {}
    total: dict[str, int] = {}
    for index, hit in enumerate(hits):
        for text in (hit.content, hit.context or ""):
            for match in _MINED_IDENTIFIER.finditer(text):
                token = match.group()
                lowered = token.lower()
                if lowered in _IDENTIFIER_NOISE:
                    continue
                # A plain lowercase word needs some length to look like a symbol.
                if token.islower() and "_" not in token and len(token) < 6:
                    continue
                per_hit.setdefault(token, set()).add(index)
                total[token] = total.get(token, 0) + 1
    return sorted(
        per_hit,
        key=lambda token: (-len(per_hit[token]), -total[token], token),
    )


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
    intent: QueryIntent = QueryIntent.FEATURE
    strategy: RetrievalStrategy = field(default_factory=RetrievalStrategy)
    use_path_index: bool = False

    # plan
    queries: list[str] = field(default_factory=list)
    planned: list[tuple[str, int]] = field(default_factory=list)
    path_queries: tuple[str, ...] = ()
    vectors: dict[str, list[float]] = field(default_factory=dict)
    embed_error: Exception | None = None

    # recall
    dense: list[SearchHit] = field(default_factory=list)
    dense_error: Exception | None = None
    exact: list[SearchHit] = field(default_factory=list)
    # Definition/endpoint chunks of the queried identifiers. Reference queries
    # recall every occurrence kind, so the declaration must be told apart
    # from the use sites the question actually asks for.
    definitions: list[SearchHit] = field(default_factory=list)
    lexical: list[SearchHit] = field(default_factory=list)
    # Compound requests: definition chunks of the identifiers the text names
    # that are declared in few enough places to be unambiguous.
    anchors: list[SearchHit] = field(default_factory=list)
    path_scores: dict[str, float] = field(default_factory=dict)
    lookup_scores: dict[str, float] = field(default_factory=dict)
    # Candidate chunks whose only symbol evidence is imports: file headers.
    # None when the exact store cannot tell.
    header_keys: frozenset[tuple[str, str]] | None = None

    # fuse / prior / rerank / select / expand
    candidates: list[SearchHit] = field(default_factory=list)
    decision: RerankDecision | None = None
    selected: list[SearchHit] = field(default_factory=list)
    related: list[SearchHit] = field(default_factory=list)

    @property
    def allowed_blob_names(self) -> frozenset[str] | None:
        return self.scope.blob_names if self.scope is not None else None

    def stage(self, name: str):
        return self.audit.stage(name) if self.audit is not None else _noop_stage(name)


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


_QUALIFIER_SEPARATORS = ("::", ".")


def split_qualified_identifiers(
    identifiers: Sequence[str],
) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]]]:
    """``Session.get`` -> look up ``get`` and remember that it must sit in ``Session``.

    The qualified spelling is kept as well: a symbol table may hold dotted
    names verbatim. The qualifier map only records real scopes, so a bare
    identifier contributes nothing to it.
    """
    lookup: list[str] = []
    qualifiers: dict[str, list[str]] = {}
    for identifier in identifiers:
        if identifier not in lookup:
            lookup.append(identifier)
        for separator in _QUALIFIER_SEPARATORS:
            if separator in identifier:
                scope, leaf = identifier.rsplit(separator, 1)
                scope_leaf = scope.rsplit(separator, 1)[-1]
                if leaf and scope_leaf:
                    if leaf not in lookup:
                        lookup.append(leaf)
                    bucket = qualifiers.setdefault(leaf, [])
                    if scope_leaf not in bucket:
                        bucket.append(scope_leaf)
                break
    return tuple(lookup), {leaf: tuple(scopes) for leaf, scopes in qualifiers.items()}


def _word_in(word: str, text: str) -> bool:
    return (
        re.search(rf"(?<![A-Za-z0-9_]){re.escape(word)}(?![A-Za-z0-9_])", text)
        is not None
    )


def resolve_qualified_hits(
    hits: list[SearchHit], qualifiers: dict[str, tuple[str, ...]]
) -> list[SearchHit]:
    """Keep the declarations that live in the requested scope.

    The scope chain (``class Session > def get``) and the file path are the
    structural evidence; the chunk text is consulted only when neither names
    the qualifier (Go receivers, C++ ``Type::method`` definitions). When no
    hit matches, the request may have named a scope the index does not know,
    so every hit stays.
    """
    if not qualifiers or not hits:
        return hits
    wanted = tuple(dict.fromkeys(q for scopes in qualifiers.values() for q in scopes))

    def structural(hit: SearchHit) -> bool:
        text = f"{hit.context or ''}\n{hit.path.replace('/', ' ')}"
        return any(_word_in(q, text) for q in wanted)

    def textual(hit: SearchHit) -> bool:
        return any(_word_in(q, hit.content) for q in wanted)

    for predicate in (structural, textual):
        matched = [hit for hit in hits if predicate(hit)]
        if matched:
            return matched
    return hits


def order_by_comentions(hits: list[SearchHit], names: Sequence[str]) -> list[SearchHit]:
    """Stable order by how many of the request's other names a chunk mentions.

    Overloads share a name; the parameter types the request spells out
    (``JsonReader``, ``TypeToken``) pick the right one deterministically.
    """
    names = tuple(dict.fromkeys(name for name in names if name))
    if len(hits) < 2 or not names:
        return hits

    def mentions(hit: SearchHit) -> int:
        text = f"{hit.context or ''}\n{hit.content}"
        return sum(_word_in(name, text) for name in names)

    return sorted(hits, key=lambda hit: -mentions(hit))


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
        except BaseException:
            for task in sql_lanes:
                task.cancel()
            # Merely cancelling background tasks leaves their exceptions and
            # database contexts pending. Drain every lane before propagating
            # planning failures or caller cancellation.
            await asyncio.gather(*sql_lanes, return_exceptions=True)
            raise
        await self._recall(state, sql_lanes)
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
        state.lookup_identifiers, state.qualifiers = split_qualified_identifiers(
            state.evidence.identifiers
        )
        state.intent = classify_query_intent(state.query)
        state.strategy = get_strategy(state.intent)
        logger.debug(
            "Query intent: {}, strategy: {}", state.intent.value, state.strategy
        )
        # 路径索引回答「哪个文件」，内容索引回答「文件里哪一段」；两者只在
        # 文件定位类查询上并行召回，再按 chunk 粒度合并。
        state.use_path_index = self.path_store is not None and (
            state.strategy.enable_path_index
            or should_use_path_index(state.query, state.intent)
        )
        if state.audit is not None:
            state.audit.intent = state.intent.value
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
        try:
            with state.stage("embed"):
                state.vectors = await self._embed_query_vectors(
                    [*state.path_queries, *(item[0] for item in state.planned)]
                )
        except Exception as exc:
            if not self._has_fallback_recall(state):
                raise
            logger.warning(
                "Query embedding failed; trying SQL retrieval: {}",
                type(exc).__name__,
            )
            state.embed_error = exc

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
        exact_task, lookup_task, lexical_task, anchor_task = sql_lanes
        (
            (state.dense, state.dense_error),
            state.path_scores,
            (state.exact, state.definitions),
            state.lookup_scores,
            state.lexical,
            state.anchors,
        ) = await asyncio.gather(
            self._recall_dense(state),
            self._recall_paths(state),
            exact_task,
            lookup_task,
            lexical_task,
            anchor_task,
        )
        if not state.lexical and self._should_recall_lexical_fallback(state):
            state.lexical = await self._recall_lexical(state, routed=True)

    async def _recall_dense(
        self, state: RetrievalState
    ) -> tuple[list[SearchHit], Exception | None]:
        if state.embed_error is not None:
            return [], state.embed_error
        try:
            with state.stage("dense"):
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
                (self.exact_store is not None and bool(evidence.identifiers))
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
    ) -> tuple[list[SearchHit], list[SearchHit]]:
        """``(occurrences, definitions)`` for the query identifiers.

        Reference questions want every occurrence kind including imports and
        additionally need to know which of those chunks declare the symbol;
        everything else asks for the structural definition only.
        """
        evidence = state.evidence
        if (
            not self.settings.exact_enabled
            or self.exact_store is None
            or state.scope is None
            or not state.scope.blob_names
            or evidence is None
            or not evidence.identifiers
        ):
            return [], []
        store = self.exact_store
        scope = state.scope
        top_k = self.settings.default_top_k
        identifiers = state.lookup_identifiers or evidence.identifiers

        async def lookup_one(
            identifier: str, kinds: Sequence[str] | None
        ) -> list[SearchHit]:
            hits = await store.search_exact(
                identifiers=(identifier,), scope=scope, top_k=top_k, kinds=kinds
            )
            # A qualified request can share a query with unrelated bare names
            # (for example ``Session.get`` plus ``Cache``). Filter only the
            # qualified identifier's own batch; applying one scope predicate to
            # the combined result would silently discard the bare name.
            for leaf, scopes in state.qualifiers.items():
                if identifier == leaf or any(
                    identifier.endswith(f"{separator}{leaf}")
                    for separator in _QUALIFIER_SEPARATORS
                ):
                    return resolve_qualified_hits(hits, {leaf: scopes})
            return hits

        async def lookup(kinds: Sequence[str] | None) -> list[SearchHit]:
            if not state.qualifiers:
                return await store.search_exact(
                    identifiers=identifiers, scope=scope, top_k=top_k, kinds=kinds
                )
            batches = await asyncio.gather(
                *(lookup_one(identifier, kinds) for identifier in identifiers)
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

        try:
            with state.stage("exact"):
                if state.intent == QueryIntent.REFERENCE:
                    occurrences, definitions = await asyncio.gather(
                        lookup(None), lookup(DEFINITION_KINDS)
                    )
                elif state.intent == QueryIntent.CALL_CHAIN:
                    occurrences, definitions = await asyncio.gather(
                        lookup((*DEFINITION_KINDS, CALL_KIND)), lookup(DEFINITION_KINDS)
                    )
                else:
                    definitions = await lookup(DEFINITION_KINDS)
                    occurrences = definitions
        except Exception as exc:
            logger.warning(
                "Exact identifier recall failed; using semantic candidates: {}", exc
            )
            return [], []
        # A qualified request (``Session.get``) pins the leaf to a scope; the
        # filtering was applied to that identifier's batch above. Among the
        # remaining declarations, the ones that mention the request's other
        # names (parameter types of an overload) come first.
        if state.intent == QueryIntent.SYMBOL:
            # The request's other names: further identifiers and the scopes of
            # qualified ones. The looked-up name itself is in every hit.
            others = [
                *evidence.identifiers[1:],
                *(scope for scopes in state.qualifiers.values() for scope in scopes),
            ]
            occurrences = order_by_comentions(occurrences, others)
            definitions = order_by_comentions(definitions, others)
        if state.audit is not None:
            state.audit.exact_definitions = len(definitions)
            state.audit.definition_sites = len(definitions)
        return occurrences, definitions

    async def _recall_anchors(self, state: RetrievalState) -> list[SearchHit]:
        """Definition chunks of the identifiers an issue-style request names.

        A compound request mixes prose, tracebacks and code names; the names
        it spells out are its strongest deterministic signal, exactly as they
        are for a symbol request. Only identifiers declared in at most three
        places qualify, the same ambiguity bound the related-definition
        expansion uses, so a traceback frame called ``send`` anchors nothing.
        """
        evidence = state.evidence
        if (
            state.intent != QueryIntent.COMPOUND
            or self.settings.compound_anchor_slots <= 0
            or not self.settings.exact_enabled
            or self.exact_store is None
            or state.scope is None
            or not state.scope.blob_names
            or evidence is None
            or not evidence.identifiers
        ):
            return []
        try:
            with state.stage("exact"):
                definitions = await self.exact_store.find_definitions(
                    identifiers=state.lookup_identifiers or evidence.identifiers,
                    scope=state.scope,
                    max_per_identifier=3,
                )
        except Exception as exc:
            logger.warning("Anchor definition recall failed: {}", exc)
            return []
        anchors: list[SearchHit] = []
        seen: set[SearchHitKey] = set()
        for definition in definitions:
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
        if state.intent == QueryIntent.SYMBOL:
            return not state.exact
        if state.intent == QueryIntent.PATH:
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
        if state.intent != QueryIntent.SYMBOL or not evidence.identifiers:
            return evidence.terms

        # Exact lookup already tried the identifier itself. Its lexical fallback
        # should use the joined surrogate (TargetService -> targetservice), not
        # broad sub-words such as target/service that dominate large term indexes.
        # Qualified identifiers use separate index tokens, so retain each part.
        terms: list[str] = []
        for identifier in evidence.identifiers:
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
        if state.intent != QueryIntent.REFERENCE or evidence is None:
            return ()
        required: list[str] = []
        for identifier in evidence.identifiers:
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
            with state.stage("path"):
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
            if state.lexical:
                hits = self._fuse_lists(
                    [hits] if hits else [],
                    state.lexical,
                )
            hits = self._merge_exact_hits(state.intent, state.exact, hits)
            if state.anchors:
                # Anchored definitions must be in the window the head rules
                # order; their own recall score is not comparable to RRF.
                present = {search_hit_key(hit) for hit in hits}
                hits = [
                    *hits,
                    *(
                        hit
                        for hit in state.anchors
                        if search_hit_key(hit) not in present
                    ),
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
                if state.use_path_index or not hits or state.intent == QueryIntent.PATH:
                    backfill |= set(state.lookup_scores)
                hits = await self._merge_path_and_content(boosts, hits, backfill)
            elif state.use_path_index:
                logger.info("No path results, using content-only")
            hits = self._filter_qualified_candidates(state, hits)
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
        if state.intent != QueryIntent.SYMBOL or not state.qualifiers or not hits:
            return hits
        resolved = resolve_qualified_hits(hits, state.qualifiers)
        if len(resolved) < len(hits):
            return resolved
        return hits

    def _fuse_lists(
        self,
        dense_lists: list[list[SearchHit]],
        lexical: list[SearchHit] | None = None,
    ) -> list[SearchHit]:
        """Weighted reciprocal rank fusion over dense facet lists plus lexical.

        Raw scores never mix: dense cosine, BM25/ts_rank and RRF do not share a
        scale, so every list contributes by rank only. A single dense list with
        no lexical companion is returned untouched so cosine scores survive for
        the confidence floor.
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
            or (state.intent != QueryIntent.COMPOUND and asks_about_tests(state.query))
            else self.priority_factor
        )
        boosted = self._working_set(state.scope)

        # Static source priors prepare the candidate order. Model rerankers run
        # afterwards, so their returned order cannot be silently overwritten.
        hits = self._apply_source_priority(
            state.candidates, priority_factor=priority_factor, boosted=boosted
        )
        await self._mark_header_chunks(state, hits)
        hits = self._prefer_source_head(state, hits, priority_factor)
        structural_heads = self._structural_heads(state, hits, priority_factor)
        if state.audit is not None:
            state.audit.head_slots = len(structural_heads)
        # This optional floor belongs to recall, before model scores can enter the
        # list. Dedicated relevance scores, dense cosine, and RRF are not calibrated
        # to a shared scale; filtering their mixture after reranking is undefined.
        # A deterministic exact symbol/path answer is protected for the same reason.
        hits = self._apply_confidence_floor(
            hits,
            priority_factor=priority_factor,
            protected=structural_heads,
        )
        hits = self._promote_heads(hits, structural_heads)

        decision = plan_rerank(
            state.intent,
            len(hits),
            has_exact_hits=bool(state.exact),
            # Embedding path similarity is useful recall but not deterministic
            # evidence. Only an exact SQL path/basename match may skip reranking.
            has_path_hits=bool(state.lookup_scores),
            definition_sites=len(state.definitions),
            head_slots=len(structural_heads),
            rerank_ambiguous_definitions=self.settings.rerank_ambiguous_definitions,
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
        # ``always`` is an evaluation/quality policy, not permission to erase a
        # deterministic answer. Rerank the full candidate set, then restore the
        # bounded structural slots while preserving the model's tail order.
        # The source head is reapplied for focused/use-site retrieval: a small
        # dedicated reranker can otherwise lead with a test, change log, issue
        # template, or the declaration when the query asks for uses. Overview
        # requests are different: source slots prepare the candidate window,
        # but an enabled semantic reranker may legitimately put architecture
        # documentation back first.
        if state.intent != QueryIntent.OVERVIEW:
            hits = self._prefer_source_head(state, hits, priority_factor)
        state.candidates = self._promote_heads(hits, structural_heads)

    async def _mark_header_chunks(
        self, state: RetrievalState, hits: Sequence[SearchHit]
    ) -> None:
        """Record which candidates are import-only file headers (one SQL lookup).

        A chunk whose recorded symbol evidence is imports and nothing else is
        the top of a file: ``use``/``import`` lines, a license comment, a
        module docstring. It names every module the file touches, which is
        why it sits close to architecture and flow questions in vector space,
        and it implements none of them. Chunks with no evidence at all are
        left alone: a script body or a config block may be the answer.
        """
        if state.header_keys is not None:
            return
        lookup = getattr(self.exact_store, "occurrence_kinds", None)
        if (
            not self.settings.head_skips_import_headers
            or lookup is None
            or state.scope is None
            or state.intent in (QueryIntent.SYMBOL, QueryIntent.PATH)
        ):
            return
        occurrences = tuple(
            dict.fromkeys(
                (hit.blob_name, hit.content_hash)
                for hit in hits
                if hit.blob_name and hit.content_hash
            )
        )
        if not occurrences:
            return
        try:
            kinds = await lookup(occurrences, state.scope)
        except Exception as exc:
            logger.warning("Occurrence lookup for head slots failed: {}", exc)
            return
        header_kinds = frozenset(HEADER_KINDS)
        state.header_keys = frozenset(
            key for key, seen in kinds.items() if seen and seen <= header_kinds
        )

    def _prefer_source_head(
        self,
        state: RetrievalState,
        hits: list[SearchHit],
        priority_factor: Callable[[str], float],
    ) -> list[SearchHit]:
        """Give the first slots to undemoted source files, in their own order.

        A test or documentation chunk that leads both the dense and the
        lexical list keeps a normalized RRF score no multiplicative prior can
        undercut, yet the request almost never asks for it first. Reference
        questions additionally keep the symbol's own declaration out of those
        slots: the question is where it is used. The demoted hits are not
        dropped; they follow immediately after the reserved slots.
        """
        slots = self.settings.source_head_slots
        if (
            slots > 0
            and state.intent == QueryIntent.REFERENCE
            and asks_about_tests(state.query)
        ):
            # "Which tests cover X": the evidenced use sites inside test files
            # are the answer, so they take the head instead of yielding it.
            evidenced = {search_hit_key(hit) for hit in (*state.exact, *state.lexical)}
            head = [
                hit
                for hit in hits
                if search_hit_key(hit) in evidenced and is_test_path(hit.path)
            ][:slots]
            if head:
                head_keys = {search_hit_key(hit) for hit in head}
                return [
                    *head,
                    *(hit for hit in hits if search_hit_key(hit) not in head_keys),
                ]
        # Symbol/path answers have their own structural heads. Broad semantic
        # requests, including overviews, reserve a few implementation slots;
        # documentation remains in the tail and coverage selection can retain it.
        if (
            slots <= 0
            or priority_factor is neutral_priority_factor
            or state.intent in (QueryIntent.SYMBOL, QueryIntent.PATH)
        ):
            return hits
        # "Where is X used" wants other places: the declaring file as a whole
        # yields the head, including its own internal calls. Only exact/lexical
        # occurrence evidence may claim a reference head slot; a dense source
        # hit that never names the identifier is not a deterministic use site.
        declaring_blobs: set[str] = set()
        reference_keys: set[SearchHitKey] | None = None
        if state.intent == QueryIntent.REFERENCE:
            declaring_blobs = {hit.blob_name for hit in state.definitions}
            reference_keys = {
                search_hit_key(hit) for hit in (*state.exact, *state.lexical)
            }

        def eligible(hit: SearchHit) -> bool:
            return (
                # Root README intentionally keeps a neutral multiplicative
                # prior, but it remains documentation and must not consume a
                # slot reserved for implementation code.
                not _is_root_readme(hit.path)
                and hit.blob_name not in declaring_blobs
                and (reference_keys is None or search_hit_key(hit) in reference_keys)
            )

        def is_header(hit: SearchHit) -> bool:
            return (
                state.header_keys is not None
                and (hit.blob_name, hit.content_hash) in state.header_keys
            )

        source = [
            hit for hit in hits if eligible(hit) and priority_factor(hit.path) >= 1.0
        ]
        head: list[SearchHit] = []
        if reference_keys is None:
            head = [hit for hit in source if not is_header(hit)][:slots]
            if not head:
                head = source[:slots]
        elif source or not self.settings.reference_head_fallback:
            head = source[:slots]
        else:
            # Use sites that exist only in tests, examples or package
            # __init__ files are still deterministic use sites; the tiers
            # keep source-like files ahead of test files within the head.
            evidenced = [hit for hit in hits if eligible(hit)]
            evidenced.sort(key=lambda hit: -priority_factor(hit.path))
            head = evidenced[:slots]
        if not head:
            return hits
        head_keys = {search_hit_key(hit) for hit in head}
        return [*head, *(hit for hit in hits if search_hit_key(hit) not in head_keys)]

    def _structural_heads(
        self,
        state: RetrievalState,
        hits: Sequence[SearchHit],
        priority_factor: Callable[[str], float] | None = None,
    ) -> tuple[SearchHitKey, ...]:
        """Bounded deterministic answers protected from score mixing.

        Up to three definitions cover overloads or duplicate declarations. A
        path request may legitimately match several files, so it reserves one
        best chunk per SQL path match up to the final result count. A compound
        request reserves a couple of slots for the definitions of the
        unambiguous identifiers its text names, in source files only.
        """
        if state.intent == QueryIntent.COMPOUND and state.anchors:
            factor = priority_factor or self.priority_factor
            anchor_keys = {search_hit_key(hit) for hit in state.anchors}
            return tuple(
                search_hit_key(hit)
                for hit in hits
                if search_hit_key(hit) in anchor_keys and factor(hit.path) >= 1.0
            )[: self.settings.compound_anchor_slots]
        if state.intent == QueryIntent.SYMBOL and state.exact:
            exact_keys = {search_hit_key(hit) for hit in state.exact}
            # ``hits`` has already received source priority. Choose within the
            # structural lane from that order so a definition in real source
            # beats the same signature shown in a documentation code block.
            # Every declaring file gets a slot before a file gets its second
            # (overloads), so two implementations are both visible.
            ordered = [hit for hit in hits if search_hit_key(hit) in exact_keys]
            slots = min(3, self.settings.final_select_k)
            heads: list[SearchHitKey] = []
            seen_blobs: set[str] = set()
            for hit in ordered:
                if hit.blob_name in seen_blobs:
                    continue
                seen_blobs.add(hit.blob_name)
                heads.append(search_hit_key(hit))
                if len(heads) >= slots:
                    break
            for hit in ordered:
                if len(heads) >= slots:
                    break
                key = search_hit_key(hit)
                if key not in heads:
                    heads.append(key)
            return tuple(heads)
        if state.intent == QueryIntent.PATH and state.lookup_scores:
            heads: list[SearchHitKey] = []
            blob_names = sorted(
                state.lookup_scores,
                key=lambda name: -state.lookup_scores[name],
            )
            for blob_name in blob_names:
                for hit in hits:
                    if hit.blob_name == blob_name:
                        heads.append(search_hit_key(hit))
                        break
                if len(heads) >= self.settings.final_select_k:
                    break
            return tuple(heads)
        return ()

    @staticmethod
    def _promote_heads(
        hits: list[SearchHit],
        heads: Sequence[SearchHitKey],
    ) -> list[SearchHit]:
        if not heads:
            return hits
        order = {key: index for index, key in enumerate(heads)}
        tail = len(order)
        return sorted(hits, key=lambda hit: order.get(search_hit_key(hit), tail))

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

    def _apply_source_priority(
        self,
        hits: list[SearchHit],
        priority_factor: Callable[[str], float] | None = None,
        boosted: frozenset[str] = frozenset(),
    ) -> list[SearchHit]:
        """按 score × 路径先验 × 工作集先验稳定重排（只重排，不改 rerank 决策）。

        普通查询默认用 self.priority_factor（源码优先）；路径类查询可传
        文档中立的 factor（neutral_priority_factor）。
        """
        factor = priority_factor or self.priority_factor
        boost = self.settings.working_set_boost

        def effective(hit: SearchHit) -> float:
            score = hit.score * factor(hit.path)
            if hit.blob_name in boosted:
                score *= boost
            return score

        return sorted(hits, key=effective, reverse=True)

    def _apply_confidence_floor(
        self,
        hits: list[SearchHit],
        priority_factor: Callable[[str], float] | None = None,
        protected: Collection[SearchHitKey] = (),
    ) -> list[SearchHit]:
        """逐条按有效分（score × penalty）剔除低于门槛的弱匹配"""
        factor = priority_factor or self.priority_factor
        floor = self.settings.confidence_floor
        return [
            hit
            for hit in hits
            if search_hit_key(hit) in protected or hit.score * factor(hit.path) >= floor
        ]

    # ── select ───────────────────────────────────────────────────────────

    async def _select(self, state: RetrievalState) -> None:
        with state.stage("select"):
            state.selected = await self.selector.select(
                state.candidates,
                self.settings.final_select_k,
                mode=state.strategy.selection_mode,
                max_chars=self._selection_budget(state),
            )

    def _context_budget(self, state: RetrievalState) -> int:
        if state.strategy.selection_mode == SelectionMode.FOCUSED:
            return self.settings.focused_max_context_chars
        return self.settings.max_context_chars

    def _selection_budget(self, state: RetrievalState) -> int | None:
        """Select primary evidence against the full budget.

        Relation evidence is optional and only becomes useful after its SQL
        lookups return something novel. Reserving a fixed block before those
        lookups can discard primary context for a query with no relations.
        ``_expand`` creates room only when it has evidence to spend.
        """
        return self._context_budget(state)

    def _expands_relations(self, state: RetrievalState) -> bool:
        strategy = state.strategy
        if state.scope is None or not state.scope.blob_names:
            return False
        outbound = (
            self.settings.related_definitions_enabled
            and strategy.expand_related_definitions
            and self.exact_store is not None
        )
        return outbound or bool(self._relation_lanes(state))

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
                    self._related_definitions(
                        state, budget=max(remaining, relation_cap)
                    )
                    if wants_related
                    else _no_hits()
                ),
                *lane_tasks,
                return_exceptions=True,
            )
            related = results[0] if isinstance(results[0], list) else []
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
            preview = assemble_sections(
                selected=state.selected,
                related=related,
                sections=sections,
                remaining_chars=relation_cap if relation_cap > 0 else max(remaining, 0),
                snippet_lines=settings.relation_snippet_lines,
            )
            if preview.hits and preview.chars > remaining:
                self._make_relation_room(state, min(preview.chars, relation_cap))
                remaining = self._context_budget(state) - sum(
                    len(hit.content) for hit in state.selected
                )
                if wants_related:
                    related = await self._related_definitions(
                        state, budget=min(remaining, relation_cap)
                    )
            # A tiny tail budget produces fragmented signatures that cost
            # another SQL lookup without explaining a relationship.
            if remaining < _MIN_RELATED_BUDGET:
                return
            pack = assemble_sections(
                selected=state.selected,
                related=related,
                sections=sections,
                remaining_chars=(
                    min(remaining, relation_cap) if relation_cap > 0 else remaining
                ),
                snippet_lines=settings.relation_snippet_lines,
            )
            state.related = pack.hits
            if state.audit is not None:
                state.audit.relation_counts = dict(pack.counts)
                state.audit.relation_chars = pack.chars

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
            if state.intent == QueryIntent.CALL_CHAIN:

                async def fetch_callers(
                    names: Sequence[str],
                ) -> list[RelatedOccurrence]:
                    return await self._call_chain_callers(state, names, scope, store)
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
        if strategy.expand_implementations and settings.implementations_enabled:
            lanes.append(
                _RelationLane(
                    "implementation",
                    lambda names: store.find_implementations(
                        identifiers=names,
                        scope=scope,
                        limit=settings.implementations_max * 2,
                    ),
                    settings.implementations_max,
                    settings.implementations_max_chars,
                )
            )
        if strategy.expand_tests and settings.tests_enabled:
            lanes.append(
                _RelationLane(
                    "test",
                    lambda names: store.find_test_uses(
                        identifiers=names, scope=scope, limit=settings.tests_max * 2
                    ),
                    settings.tests_max,
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

    async def _call_chain_callers(
        self,
        state: RetrievalState,
        identifiers: Sequence[str],
        scope: SearchScope,
        store: RelationStore,
    ) -> list[RelatedOccurrence]:
        direct = await store.find_callers(
            identifiers=identifiers,
            scope=scope,
            limit=max(self.settings.callers_max * 2, self.settings.callers_max),
        )
        direct = [replace(item, hop=1) for item in direct]
        by_hop: dict[int, list[RelatedOccurrence]] = {1: direct}
        if self.settings.call_chain_max_hops <= 1 or self.exact_store is None:
            return direct

        seen_names = set(identifiers)
        seen_spans = {
            (item.hit.blob_name, item.hit.start_line, item.hit.end_line, item.enclosing)
            for item in direct
        }
        frontier = tuple(
            dict.fromkeys(
                item.enclosing
                for item in direct
                if item.enclosing and item.enclosing not in seen_names
            )
        )
        for hop in range(2, self.settings.call_chain_max_hops + 1):
            if not frontier:
                break
            definitions = await self.exact_store.find_definitions(
                identifiers=frontier,
                scope=scope,
                max_per_identifier=1,
            )
            resolvable = tuple(dict.fromkeys(item.identifier for item in definitions))
            if not resolvable:
                break
            next_occurrences = await store.find_callers(
                identifiers=resolvable,
                scope=scope,
                limit=max(self.settings.callers_max * 2, self.settings.callers_max),
            )
            current: list[RelatedOccurrence] = []
            next_frontier: list[str] = []
            for occurrence in next_occurrences:
                key = (
                    occurrence.hit.blob_name,
                    occurrence.hit.start_line,
                    occurrence.hit.end_line,
                    occurrence.enclosing,
                )
                if key in seen_spans:
                    continue
                seen_spans.add(key)
                current.append(replace(occurrence, hop=hop))
                if occurrence.enclosing and occurrence.enclosing not in seen_names:
                    next_frontier.append(occurrence.enclosing)
            if not current:
                break
            by_hop[hop] = current
            seen_names.update(resolvable)
            frontier = tuple(dict.fromkeys(next_frontier))

        ordered: list[RelatedOccurrence] = []
        for index in range(self.settings.callers_max * 2):
            for hop in sorted(by_hop):
                values = by_hop[hop]
                if index < len(values):
                    ordered.append(values[index])
        return ordered

    async def _related_definitions(
        self, state: RetrievalState, *, budget: int | None = None
    ) -> list[SearchHit]:
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
            return []
        related_budget = min(settings.related_max_chars, available)
        sources = state.selected[: settings.related_source_hits]
        ordered: list[str] = []
        for identifier in (
            *state.lookup_identifiers,
            *_mine_identifiers(sources),
        ):
            if identifier not in ordered:
                ordered.append(identifier)
        # Symbols defined by the selected code itself need no pull-in; the
        # store returns their chunk so the filter below drops them.
        candidates = ordered[: settings.related_max_symbols * 5]
        if not candidates:
            return []

        definitions = await self.exact_store.find_definitions(
            identifiers=candidates,
            scope=state.scope,
            max_per_identifier=settings.related_max_definitions_per_symbol,
        )
        selected_keys = {(hit.blob_name, hit.content_hash) for hit in state.selected}
        selected_spans = [
            (hit.blob_name, hit.start_line, hit.end_line) for hit in state.selected
        ]
        by_identifier: dict[str, list[DefinitionHit]] = {}
        for definition in definitions:
            hit = definition.hit
            if (hit.blob_name, hit.content_hash) in selected_keys:
                continue
            if any(
                blob == hit.blob_name and start <= definition.start_line <= end
                for blob, start, end in selected_spans
            ):
                continue
            by_identifier.setdefault(definition.identifier, []).append(definition)

        related: list[SearchHit] = []
        seen: set[tuple[str, int]] = set()
        used_chars = 0
        symbols = 0
        for identifier in candidates:
            if identifier not in by_identifier:
                continue
            if symbols >= settings.related_max_symbols:
                break
            added = False
            for definition in by_identifier[identifier]:
                key = (definition.hit.blob_name, definition.start_line)
                if key in seen:
                    continue
                excerpt = definition_excerpt(definition, settings.related_snippet_lines)
                if excerpt is None:
                    continue
                if used_chars + len(excerpt.content) > related_budget:
                    continue
                seen.add(key)
                related.append(excerpt)
                used_chars += len(excerpt.content)
                added = True
            if added:
                symbols += 1
        return related


def definition_excerpt(definition: DefinitionHit, max_lines: int) -> SearchHit | None:
    """Cut the first ``max_lines`` lines of a definition out of its chunk."""
    chunk = definition.hit
    lines = chunk.content.splitlines()
    offset = definition.start_line - chunk.start_line
    if offset < 0 or offset >= len(lines):
        return None
    span = min(definition.end_line - definition.start_line + 1, max_lines)
    excerpt = lines[offset : offset + span]
    while excerpt and not excerpt[-1].strip():
        excerpt.pop()
    if not excerpt:
        return None
    return replace(
        chunk,
        content="\n".join(excerpt),
        start_line=definition.start_line,
        end_line=definition.start_line + len(excerpt) - 1,
        score=0.0,
        role="related",
    )


def merge_adjacent_hits(hits: Sequence[SearchHit]) -> list[SearchHit]:
    """Join hits of one file whose line spans touch or overlap.

    The merged hit keeps the rank position of its best member and stitches
    text by line number, so the result renders exactly like the source.
    """
    groups: dict[tuple[str, str], list[int]] = {}
    for index, hit in enumerate(hits):
        groups.setdefault((hit.blob_name, hit.path), []).append(index)

    merged_at: dict[int, SearchHit] = {}
    consumed: set[int] = set()
    for indices in groups.values():
        ordered = sorted(indices, key=lambda index: hits[index].start_line)
        cluster: list[int] = []
        cluster_end = 0
        for index in ordered:
            hit = hits[index]
            if cluster and hit.start_line <= cluster_end + 1:
                cluster.append(index)
                cluster_end = max(cluster_end, hit.end_line)
                continue
            if cluster:
                _emit_cluster(hits, cluster, merged_at, consumed)
            cluster = [index]
            cluster_end = hit.end_line
        if cluster:
            _emit_cluster(hits, cluster, merged_at, consumed)

    return [
        merged_at[index]
        for index in range(len(hits))
        if index in merged_at and index not in consumed
    ]


def _emit_cluster(
    hits: Sequence[SearchHit],
    cluster: list[int],
    merged_at: dict[int, SearchHit],
    consumed: set[int],
) -> None:
    anchor = min(cluster)
    if len(cluster) == 1:
        merged_at[anchor] = hits[anchor]
        return
    lines: dict[int, str] = {}
    end_line = 0
    for index in cluster:
        hit = hits[index]
        for offset, text in enumerate(hit.content.splitlines()):
            lines.setdefault(hit.start_line + offset, text)
        end_line = max(end_line, hit.end_line)
    start_line = hits[cluster[0]].start_line
    content = "\n".join(
        lines.get(number, "") for number in range(start_line, end_line + 1)
    )
    best = max((hits[index] for index in cluster), key=lambda hit: hit.score)
    contexts = {hits[index].context for index in cluster}
    merged_at[anchor] = replace(
        hits[cluster[0]],
        content=content,
        start_line=start_line,
        end_line=end_line,
        score=best.score,
        content_hash="",
        context=contexts.pop() if len(contexts) == 1 else None,
    )
    consumed.update(index for index in cluster if index != anchor)
