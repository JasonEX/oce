"""The records one retrieval carries through its stages.

A retrieval is a fixed sequence of transitions; each stage produces one
record and the stages after it only read it:

    route   -> QueryRoute      what the request asks for (``route.py``)
    plan    -> QueryPlan       query variants; embedding starts or is deferred
    recall  -> RecallEvidence  what every lane found
    fuse    -> candidates      one ordered list
    rank    -> candidates      priors, bounded heads, model reorder
    select  -> selected        the primary answer within the budget
    expand  -> related         relation sections and the call chain

``RetrievalState`` holds the request, those records and two small caches of
raw facts (implementation keys, call and definition rows) that the head
restore and the relation re-render reuse within the request. ``lane_failed``
is the single place a lane reports that it raised and was skipped: the answer
still comes back, so the degradation must be written down where the audit can
see it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from types import MappingProxyType

from loguru import logger

from oce.domain.services.retrieval.route import QueryRoute
from oce.domain.services.retrieval_strategy import RerankDecision
from oce.domain.services.search import (
    DefinitionHit,
    SearchHit,
    SearchHitKey,
    SearchScope,
)
from oce.shared.metrics import RetrievalAudit

# Query vectors by query text, and the wall time of the embedding round trip.
EmbeddingResult = tuple[dict[str, list[float]], int]


@dataclass(frozen=True)
class QueryPlan:
    """The texts the lanes search with."""

    # The original request, or the rewrites that replace it.
    queries: tuple[str, ...] = ()
    # ``(facet, facet count of its query)``: a decomposed query recalls a
    # smaller window per facet and the facets are fused afterwards.
    facets: tuple[tuple[str, int], ...] = ()
    # Variants the semantic path index searches; empty unless routed to it.
    path_queries: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExactEvidence:
    """What the identifier lookups over ``symbol_occurrences`` found."""

    # Every occurrence the intent asks for, in lane order.
    hits: tuple[SearchHit, ...] = ()
    # Definition/endpoint chunks of the queried identifiers. Reference
    # queries recall every occurrence kind, so the declaration must be told
    # apart from the use sites the question actually asks for.
    definitions: tuple[SearchHit, ...] = ()
    # Reference queries: chunks that call or extend the queried identifiers,
    # i.e. deterministic use sites as opposed to imports or the declaration.
    use_sites: tuple[SearchHit, ...] = ()
    # Call-chain queries: the declarations of each named symbol, in query
    # order, so "how does A reach B" can start at A and stop at B.
    endpoints: tuple[tuple[str, tuple[DefinitionHit, ...]], ...] = ()
    # A symbol query may also name parameter types. Only finding the first
    # requested symbol makes the SQL answer decisive; a definition of one of
    # the disambiguating types does not.
    primary_definition_found: bool = False
    # Scope-wide recorded declaration counts, gathered only for an audited request.
    # A chunk may contain several declarations; chunk counts cannot measure ambiguity.
    definition_counts: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class RecallEvidence:
    """The output of every recall lane; later stages only read it."""

    exact: ExactEvidence = field(default_factory=ExactEvidence)
    lexical: tuple[SearchHit, ...] = ()
    # Compound requests: declarations a traceback frame or the title names.
    anchors: tuple[SearchHit, ...] = ()
    # Exact SQL path matches and semantic path-index scores, per blob.
    lookup_scores: Mapping[str, float] = field(default_factory=dict)
    path_scores: Mapping[str, float] = field(default_factory=dict)
    dense: tuple[SearchHit, ...] = ()
    dense_error: Exception | None = None
    # ``dense``, ``skip:<reason>`` when the SQL lanes answered first, or
    # ``error:<type>`` when the vector lanes failed and another lane answered.
    dense_route: str | None = None

    def __post_init__(self) -> None:
        # Copy before freezing so a lane retaining its input cannot change
        # the evidence later stages read.
        object.__setattr__(
            self, "lookup_scores", MappingProxyType(dict(self.lookup_scores))
        )
        object.__setattr__(
            self, "path_scores", MappingProxyType(dict(self.path_scores))
        )

    @property
    def dense_skipped(self) -> bool:
        return self.dense_route is not None and self.dense_route.startswith("skip:")


@dataclass
class RetrievalState:
    """One retrieval: the request, each stage's record, and per-request caches."""

    query: str
    scope: SearchScope | None
    route: QueryRoute
    audit: RetrievalAudit | None = None

    plan: QueryPlan = field(default_factory=QueryPlan)
    # None until model-backed recall is needed. A sent remote request runs
    # independently so recall can release its waiter without cancelling it.
    embedding: asyncio.Task[EmbeddingResult] | None = None
    recall: RecallEvidence = field(default_factory=RecallEvidence)

    candidates: list[SearchHit] = field(default_factory=list)
    decision: RerankDecision | None = None
    # Rank owns the bounded deterministic heads; selection must retain their
    # precedence when coverage would otherwise postpone a same-file answer.
    structural_heads: tuple[SearchHitKey, ...] = ()
    selected: list[SearchHit] = field(default_factory=list)
    related: list[SearchHit] = field(default_factory=list)

    # Rank caches. Candidate chunks whose only symbol evidence is imports
    # (file headers; None when the store cannot tell), and the impl blocks of
    # an implementors question, reused when the head is restored after rerank.
    header_keys: frozenset[tuple[str, str]] | None = None
    implementor_keys: frozenset[SearchHitKey] | None = None
    # Expand caches. Expansion may render again after trimming the primary
    # tail; only raw rows are reused, names, excerpts and budgets are recomputed.
    related_calls: dict[tuple[str, int, int], tuple[tuple[str, int, str], ...]] = field(
        default_factory=dict
    )
    related_definitions: dict[tuple[int, str], tuple[DefinitionHit, ...]] = field(
        default_factory=dict
    )

    @property
    def allowed_blob_names(self) -> frozenset[str] | None:
        return self.scope.blob_names if self.scope is not None else None

    def stage(self, name: str) -> AbstractContextManager[None]:
        """Time a stage into the audit; without an audit the block runs untimed."""
        if self.audit is None:
            return nullcontext()
        return self.audit.stage(name)


def lane_failed(state: RetrievalState, lane: str, exc: BaseException) -> None:
    """Record that ``lane`` raised and the request continued without it.

    The warning names the lane and the exception type only; the traceback is
    kept at debug level so a degraded deployment can be diagnosed with
    ``-vv`` without the default log echoing query text or SQL parameters.
    The audit entry reaches ``retrieval_metrics.lane_failures``, which is
    what tells an offline comparison a ranking change from a lane that
    silently stopped contributing.
    """
    name = type(exc).__name__
    if state.audit is not None:
        state.audit.lane_failures[lane] = name
    logger.warning("{} lane failed and was skipped: {}", lane, name)
    logger.opt(exception=exc).debug("{} lane traceback", lane)
