"""The mutable record one retrieval carries through the stages.

Every stage reads and writes only the fields it owns; the field groups
below follow the stage order. ``lane_failed`` is the single place a lane
reports that it raised and was skipped: the answer still comes back, so the
degradation must be written down where the audit can see it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, field
from time import perf_counter

from loguru import logger

from oce.domain.services.query_classifier import QueryIntent
from oce.domain.services.query_evidence import QueryEvidence
from oce.domain.services.retrieval_strategy import RerankDecision, RetrievalStrategy
from oce.domain.services.search import (
    DefinitionHit,
    HubDefinition,
    SearchHit,
    SearchScope,
)
from oce.shared.metrics import RetrievalAudit


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
    # A symbol query may also name parameter types. Only finding the first
    # requested symbol makes the SQL answer decisive; a definition of one of
    # the disambiguating types does not.
    primary_definition_found: bool = False
    # Reference queries: chunks that call or extend the queried identifiers,
    # i.e. deterministic use sites as opposed to imports or the declaration.
    use_sites: list[SearchHit] = field(default_factory=list)
    # Call-chain queries: the declarations of each named symbol, in query
    # order, so "how does A reach B" can start at A and stop at B.
    endpoints: list[tuple[str, list[DefinitionHit]]] = field(default_factory=list)
    lexical: list[SearchHit] = field(default_factory=list)
    # Compound requests: definition chunks of the identifiers the text names
    # that are declared in few enough places to be unambiguous.
    anchors: list[SearchHit] = field(default_factory=list)
    # Feature/overview requests: declared names the request's words spell,
    # most widely referenced first (``Router`` for "router composition").
    hubs: list[HubDefinition] = field(default_factory=list)
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

    def stage(self, name: str) -> AbstractContextManager[None]:
        """Time a stage into the audit; without an audit the block runs untimed."""
        if self.audit is None:
            return nullcontext()
        return self.audit.stage(name)

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
