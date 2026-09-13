"""The SQL text lanes: lexical full-text recall and exact path lookup."""

from __future__ import annotations

import asyncio

from oce.domain.services.lexical import lexical_tokens
from oce.domain.services.query_classifier import QueryIntent
from oce.domain.services.retrieval.state import RetrievalState, lane_failed
from oce.domain.services.search import LexicalSearchStore, PathLookupStore, SearchHit
from oce.shared.config.settings import RetrievalSettings


class LexicalLane:
    """Term and phrase recall over the chunk term index.

    Symbol and path requests defer this lane until their structural operator
    misses; semantic requests run it alongside the vector lanes.
    """

    def __init__(
        self, store: LexicalSearchStore | None, settings: RetrievalSettings
    ) -> None:
        self.store = store
        self.settings = settings

    def can_recall(self, state: RetrievalState) -> bool:
        evidence = state.evidence
        return bool(
            self.settings.lexical_enabled
            and self.store is not None
            and state.scope is not None
            and state.scope.blob_names
            and evidence is not None
            and (evidence.terms or evidence.phrases)
        )

    def should_recall_eagerly(self, state: RetrievalState) -> bool:
        evidence = state.evidence
        return self.can_recall(state) and bool(
            state.strategy.enable_lexical_recall
            or (evidence is not None and evidence.phrases)
        )

    def should_recall_fallback(self, state: RetrievalState) -> bool:
        if self.should_recall_eagerly(state):
            return False
        if not self.can_recall(state):
            return False
        if state.intent == QueryIntent.SYMBOL:
            return not state.exact
        if state.intent == QueryIntent.PATH:
            return not state.lookup_scores
        return False

    async def recall(self, state: RetrievalState, *, routed: bool) -> list[SearchHit]:
        evidence = state.evidence
        store = self.store
        if (
            not routed
            or not self.can_recall(state)
            or evidence is None
            or store is None
            or state.scope is None
        ):
            return []
        try:
            with state.stage("lexical"):
                async with asyncio.timeout(self.settings.lexical_timeout_seconds):
                    return await store.search_lexical(
                        terms=self.terms(state),
                        phrases=evidence.phrases,
                        scope=state.scope,
                        top_k=self.settings.lexical_top_k,
                        required=self.required(state),
                    )
        except Exception as exc:
            # A timeout is the common failure: the lane is skipped and the
            # other lanes answer, exactly as for any other error.
            lane_failed(state, "lexical", exc)
            return []

    @staticmethod
    def terms(state: RetrievalState) -> tuple[str, ...]:
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
    def required(state: RetrievalState) -> tuple[str, ...]:
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
            leaf_name = identifier.rsplit("::", 1)[-1].rsplit(".", 1)[-1]
            tokens = lexical_tokens(leaf_name)
            if not tokens:
                continue
            surrogate = max(tokens, key=len)
            if surrogate not in required:
                required.append(surrogate)
        return tuple(required)


class PathLookupLane:
    """Exact path/basename evidence (explicit filenames, traceback frames)."""

    def __init__(
        self, store: PathLookupStore | None, settings: RetrievalSettings
    ) -> None:
        self.store = store
        self.settings = settings

    async def recall(self, state: RetrievalState) -> dict[str, float]:
        evidence = state.evidence
        store = self.store
        if (
            not self.settings.path_lookup_enabled
            or store is None
            or state.scope is None
            or not state.scope.blob_names
            or evidence is None
            or not evidence.has_path_evidence
        ):
            return {}
        try:
            with state.stage("path_lookup"):
                return await store.match_paths(
                    filenames=evidence.filenames,
                    paths=evidence.paths,
                    scope=state.scope,
                    limit=self.settings.path_top_k,
                )
        except Exception as exc:
            lane_failed(state, "path_lookup", exc)
            return {}
