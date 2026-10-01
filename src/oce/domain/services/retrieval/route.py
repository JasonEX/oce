"""The route stage: what a request asks for, decided once from its text.

Routing is deterministic. The request is parsed a single time into code
identifiers, traceback frames, file names and terms; the intent, the strategy
and the narrower question flags (does it ask for tests, for implementors) are
derived from that parse and never re-read from the text by a later stage.
Models only take part in the explicit rewrite and rerank stages.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from oce.domain.services.query_classifier import (
    QueryIntent,
    asks_about_tests,
    asks_for_implementors,
    classify_query_intent,
    should_use_path_index,
)
from oce.domain.services.query_evidence import QueryEvidence, extract_query_evidence
from oce.domain.services.query_tokens import extract_code_identifiers
from oce.domain.services.retrieval.names import (
    IDENTIFIER_NOISE,
    leaf,
    split_qualified_identifiers,
    word_in,
)
from oce.domain.services.retrieval_strategy import RetrievalStrategy, get_strategy


@dataclass(frozen=True)
class QueryRoute:
    """The routed request; every later stage reads it and none rewrites it."""

    evidence: QueryEvidence
    intent: QueryIntent
    strategy: RetrievalStrategy
    # Names the structural lanes look up: every query identifier plus the leaf
    # of each qualified one (``Session.get`` -> ``get``); ``qualifiers`` maps a
    # leaf to the scopes the request pinned it to.
    lookup_identifiers: tuple[str, ...] = ()
    # Issue-title names eligible for the compound anchor lane.
    title_identifiers: tuple[str, ...] = ()
    qualifiers: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    # The path index answers "which file", the content index "which part of
    # it"; both recall only for file-locating requests.
    use_path_index: bool = False
    # A question-sized request that asks for tests ("which tests cover X").
    asks_tests: bool = False
    # "Which classes implement X" / "where is X implemented for Y".
    asks_implementors: bool = False


def route_query(query: str, *, path_index_available: bool) -> QueryRoute:
    """Parse ``query`` once and derive every routing decision from that parse."""
    code_identifiers = extract_code_identifiers(query)
    evidence = extract_query_evidence(query, code_identifiers)
    lookup_identifiers, qualifiers = split_qualified_identifiers(evidence.identifiers)
    title = query.strip().splitlines()[0] if query.strip() else ""
    title_identifiers = tuple(
        identifier
        for identifier in lookup_identifiers
        if word_in(leaf(identifier), title)
        and leaf(identifier).lower() not in IDENTIFIER_NOISE
    )
    intent = classify_query_intent(query, code_identifiers)
    strategy = get_strategy(intent)
    return QueryRoute(
        evidence=evidence,
        intent=intent,
        strategy=strategy,
        lookup_identifiers=lookup_identifiers,
        title_identifiers=title_identifiers,
        qualifiers=MappingProxyType(dict(qualifiers)),
        use_path_index=path_index_available
        and (strategy.enable_path_index or should_use_path_index(query, intent)),
        asks_tests=asks_about_tests(query),
        asks_implementors=asks_for_implementors(query),
    )
