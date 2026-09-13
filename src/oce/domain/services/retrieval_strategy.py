"""Intent-driven retrieval strategy table.

A strategy sets the deterministic recall switches; authorization and
per-query routing of both rerankers is decided by ``plan_rerank``.
"""

from __future__ import annotations

from dataclasses import dataclass

from oce.domain.services.query_classifier import QueryIntent
from oce.domain.services.selector.protocols import SelectionMode
from oce.shared.config.settings import RerankPolicy


@dataclass(frozen=True)
class RetrievalStrategy:
    """The lanes and selection mode one intent uses."""

    enable_path_index: bool = False
    enable_query_rewrite: bool = False
    enable_lexical_recall: bool = False
    expand_related_definitions: bool = False
    # Relation lanes: who calls, who implements or inherits, which tests
    # exercise, which barrel file re-exports.
    expand_callers: bool = False
    expand_implementations: bool = False
    expand_tests: bool = False
    expand_reexports: bool = False
    selection_mode: SelectionMode = SelectionMode.COVERAGE

    @property
    def expands_relations(self) -> bool:
        return (
            self.expand_related_definitions
            or self.expand_callers
            or self.expand_implementations
            or self.expand_tests
            or self.expand_reexports
        )


# intent -> strategy
STRATEGY_TABLE: dict[QueryIntent, RetrievalStrategy] = {
    # SYMBOL: the name is located in the body; path semantics would rank
    # same-named references, models and DAOs ahead of the declaration. The
    # questions that follow a definition are who calls it, who implements
    # it, which tests exercise it, where it is exported, and which symbols
    # its body refers to (factories, base classes, delegated methods).
    QueryIntent.SYMBOL: RetrievalStrategy(
        enable_path_index=False,
        enable_query_rewrite=True,
        expand_related_definitions=True,
        expand_callers=True,
        expand_implementations=True,
        expand_tests=True,
        expand_reexports=True,
        selection_mode=SelectionMode.FOCUSED,
    ),
    # CALL_CHAIN: no rewrite, because the direction and endpoints in the
    # original wording are what the reranker judges call relations by.
    # Lexical occurrences and second-hop definitions supply the structure
    # dense recall does not know.
    QueryIntent.CALL_CHAIN: RetrievalStrategy(
        enable_lexical_recall=True,
        expand_related_definitions=True,
        expand_callers=True,
        expand_tests=True,
    ),
    # REFERENCE: rewrites add synonymous ways of calling. Exact recall
    # already includes re-export and inherit rows; the callers and tests
    # sections add use sites outside the window, and the symbol's own
    # declaration is appended as an excerpt when the per-file limit pushed
    # it out of the primary results.
    QueryIntent.REFERENCE: RetrievalStrategy(
        enable_query_rewrite=True,
        enable_lexical_recall=True,
        expand_related_definitions=True,
        expand_callers=True,
        expand_tests=True,
    ),
    # PATH: rewrites bridge Chinese and English file semantics, the path
    # index recalls, and a confident match needs no LLM.
    QueryIntent.PATH: RetrievalStrategy(
        enable_path_index=True,
        enable_query_rewrite=True,
        selection_mode=SelectionMode.FOCUSED,
    ),
    # FEATURE: a behaviour description needs cross-language term recall.
    QueryIntent.FEATURE: RetrievalStrategy(
        enable_query_rewrite=True,
        enable_lexical_recall=True,
        expand_related_definitions=True,
        expand_tests=True,
    ),
    # OVERVIEW: dense recall leads; lexical hits add module and document terms.
    QueryIntent.OVERVIEW: RetrievalStrategy(
        enable_lexical_recall=True,
        expand_related_definitions=True,
    ),
    # COMPOUND: rewrites split parallel conditions into separately recalled
    # angles.
    QueryIntent.COMPOUND: RetrievalStrategy(
        enable_query_rewrite=True,
        enable_lexical_recall=True,
        expand_tests=True,
    ),
}


def get_strategy(intent: QueryIntent) -> RetrievalStrategy:
    return STRATEGY_TABLE[intent]


@dataclass(frozen=True)
class RerankDecision:
    """Which rerankers run for one candidate set, and the evidence that decided it."""

    dedicated: bool
    llm: bool
    reason: str

    @property
    def route(self) -> str:
        """Audit label: the rerankers applied, or the skip reason when none ran."""
        applied = [
            name
            for name, on in (("dedicated", self.dedicated), ("llm", self.llm))
            if on
        ]
        if applied:
            return "+".join(applied)
        return f"skip:{self.reason}"


def plan_rerank(
    intent: QueryIntent,
    candidate_count: int,
    *,
    has_exact_hits: bool = False,
    has_path_hits: bool = False,
    dense_skipped: bool = False,
    definition_sites: int = 0,
    head_slots: int = 3,
    rerank_ambiguous_definitions: bool = False,
    dedicated_enabled: bool = True,
    llm_enabled: bool = True,
    dedicated_policy: RerankPolicy = "adaptive",
    llm_policy: RerankPolicy = "adaptive",
) -> RerankDecision:
    """Decide which rerankers a candidate set benefits from.

    Both models share the same deterministic evidence. Retrieval scores are
    deliberately excluded: dense cosine, RRF, exact, path, and reranker scores do
    not share a calibrated scale, so a skip is only taken when a structural
    operator has already answered the question. ``definition_sites`` is the
    number of places declaring the queried name; it only matters when the
    deployment opted into reranking ambiguous symbol tails. ``enabled`` flags
    authorize the corresponding stage; a policy can never switch on a model
    that is disabled.
    """
    for name, policy in (
        ("dedicated rerank", dedicated_policy),
        ("LLM rerank", llm_policy),
    ):
        if policy not in ("adaptive", "always"):
            raise ValueError(f"Unsupported {name} policy: {policy}")

    if candidate_count < 2:
        return RerankDecision(False, False, "too_few_candidates")

    if (
        intent == QueryIntent.SYMBOL
        and has_exact_hits
        and rerank_ambiguous_definitions
        and definition_sites > head_slots
    ):
        # More declarations of the name than protected head slots: the head
        # stays deterministic, a dedicated model may order the overflow.
        adaptive = (True, False, "ambiguous_definition")
    elif intent == QueryIntent.SYMBOL and has_exact_hits:
        adaptive = (False, False, "exact_definition")
    elif intent == QueryIntent.PATH and has_path_hits:
        adaptive = (False, False, "path_evidence")
    elif intent in (QueryIntent.SYMBOL, QueryIntent.PATH):
        adaptive = (True, True, "no_structural_evidence")
    elif intent == QueryIntent.REFERENCE:
        # Reference questions want occurrence coverage; a global semantic judge
        # would collapse the list onto one implementation.
        adaptive = (True, False, "reference_keep_coverage")
    else:
        adaptive = (True, True, "semantic")
    if dense_skipped and (adaptive[0] or adaptive[1]):
        # The SQL lanes answered and vector recall was never awaited: the
        # candidates are the structural answer plus its lexical companions,
        # which the head rules already order. A cross-encoder pass would
        # spend a second on a list it may not reorder. ``always`` still runs.
        adaptive = (False, False, "deterministic")

    dedicated = dedicated_enabled and (dedicated_policy == "always" or adaptive[0])
    llm = llm_enabled and (llm_policy == "always" or adaptive[1])
    reason = adaptive[2]
    if not dedicated and not llm and not (dedicated_enabled or llm_enabled):
        reason = "no_reranker_enabled"
    return RerankDecision(dedicated, llm, reason)
