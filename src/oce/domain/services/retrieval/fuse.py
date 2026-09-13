"""The fuse stage: one candidate list from every lane's ranked list.

Raw scores never mix. Dense cosine, BM25/ts_rank, exact-lane scores and RRF
do not share a scale, so lists contribute by rank; the structural lanes
(exact, anchors, hubs) are merged by key, and path evidence is a bounded
boost with a backfill for files the content index never mentions.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace

from loguru import logger

from oce.domain.services.path_search import PathContentStore
from oce.domain.services.query_classifier import QueryIntent
from oce.domain.services.retrieval.hubs import hub_heads
from oce.domain.services.retrieval.names import (
    QUALIFIER_SEPARATORS,
    resolve_qualified_hits,
)
from oce.domain.services.retrieval.state import RetrievalState, lane_failed
from oce.domain.services.search import SearchHit, SearchHitKey, search_hit_key
from oce.shared.config.settings import RetrievalSettings


def fuse_lists(
    settings: RetrievalSettings,
    dense_lists: list[list[SearchHit]],
    lexical: list[SearchHit] | None = None,
    *,
    extra: Sequence[list[SearchHit]] = (),
) -> list[SearchHit]:
    """Weighted reciprocal rank fusion over dense facet lists plus lexical.

    A single dense list with no lexical companion is returned untouched so
    cosine scores survive for the confidence floor.
    """
    lists = list(dense_lists)
    weights = [1.0] + [settings.query_facet_weight] * (len(lists) - 1) if lists else []
    if lexical:
        lists.append(lexical)
        weights.append(settings.lexical_weight)
    # Structural lists (declarations of the request's identifiers) go
    # first so that, at equal fused score, deterministic evidence leads.
    for ranked in reversed([ranked for ranked in extra if ranked]):
        lists.insert(0, ranked)
        weights.insert(0, 1.0)
    if not lists:
        return []
    if len(lists) == 1:
        return lists[0]

    rrf_k = settings.rrf_k
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
        for key in keys[: settings.default_top_k]
    ]


def filter_qualified_candidates(
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
    # "Where are ``Session.get`` and ``Cache`` defined": the scope pins
    # ``get`` only; a bare name asked for alongside keeps its candidates.
    qualified = set(state.qualifiers) | {
        identifier
        for identifier in state.lookup_identifiers
        if any(sep in identifier for sep in QUALIFIER_SEPARATORS)
    }
    if any(identifier not in qualified for identifier in state.lookup_identifiers):
        return hits
    resolved = resolve_qualified_hits(hits, state.qualifiers)
    if len(resolved) < len(hits):
        return resolved
    return hits


class Fusion:
    def __init__(
        self,
        *,
        settings: RetrievalSettings,
        path_content_store: PathContentStore | None,
        priority_factor: Callable[[str], float],
        rerank_window: int | None,
    ) -> None:
        self.settings = settings
        self.path_content_store = path_content_store
        self.priority_factor = priority_factor
        # The LLM reranker's candidate window; None means unbounded. Exact
        # hits must land inside it to be reranked, otherwise a call-chain
        # request's definitions are crowded out by semantic candidates.
        self.rerank_window = rerank_window

    async def fuse(self, state: RetrievalState) -> None:
        settings = self.settings
        with state.stage("fuse"):
            hits = state.dense
            if not hits and state.dense_route and state.dense_route.startswith("skip:"):
                # Vector recall was not awaited: the exact lane is the primary
                # list, and lexical evidence joins it by rank so its raw BM25
                # scores never order the candidates on their own.
                hits = list(state.exact)
            if state.intent == QueryIntent.COMPOUND and state.exact and hits:
                # An issue names many identifiers (its example's helpers,
                # every traceback frame, the types it mentions); their
                # declarations are one more ranked list, not a score that
                # outbids the fused order. The deterministic ones become
                # anchors and take the head below.
                hits = fuse_lists(settings, [hits], state.lexical, extra=[state.exact])
            elif state.lexical:
                hits = fuse_lists(settings, [hits] if hits else [], state.lexical)
            hits = self.merge_exact_hits(state.intent, state.exact, hits)
            if state.intent == QueryIntent.CALL_CHAIN and len(state.endpoints) >= 2:
                present = {search_hit_key(hit) for hit in hits}
                for _name, definitions in state.endpoints[:2]:
                    first = definitions[0].hit
                    if search_hit_key(first) not in present:
                        hits.append(first)
                        present.add(search_hit_key(first))
            structural = [
                *state.anchors,
                *hub_heads(state.hubs, self.priority_factor, settings.hub_head_slots),
            ]
            if structural:
                # Anchored and hub definitions must be in the window the head
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
                if state.use_path_index or not hits or state.intent == QueryIntent.PATH:
                    backfill |= set(state.lookup_scores)
                hits = await self.merge_path_and_content(state, boosts, hits, backfill)
            elif state.use_path_index:
                logger.info("No path results, using content-only")
            hits = filter_qualified_candidates(state, hits)
            # Only when nothing at all was recalled does a failed dense lane
            # become the request's error.
            if not hits and state.dense_error is not None:
                raise state.dense_error
            state.candidates = hits

    def merge_exact_hits(
        self,
        intent: QueryIntent,
        exact_hits: list[SearchHit],
        semantic_hits: list[SearchHit],
    ) -> list[SearchHit]:
        settings = self.settings
        if intent == QueryIntent.SYMBOL and exact_hits:
            # A definition lookup is a structural lane, not another score to
            # calibrate against cosine/RRF. Keep its leading answer in the
            # candidate window; the rank stage restores that deterministic
            # head after source priors, while still leaving room for
            # semantic context.
            merged: list[SearchHit] = []
            seen: set[SearchHitKey] = set()
            for hit in [*exact_hits, *semantic_hits]:
                key = search_hit_key(hit)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(hit)
            return merged[: settings.default_top_k]

        if intent == QueryIntent.CALL_CHAIN and semantic_hits:
            semantic_keys = {search_hit_key(hit) for hit in semantic_hits}
            exact_only = [
                hit for hit in exact_hits if search_hit_key(hit) not in semantic_keys
            ]
            candidate_window = min(
                settings.default_top_k,
                self.rerank_window or settings.default_top_k,
            )
            reserved = min(len(exact_only), max(1, candidate_window // 3))
            semantic_slots = max(candidate_window - reserved, 1)
            anchor_index = min(semantic_slots, len(semantic_hits)) - 1
            anchor_score = semantic_hits[anchor_index].score
            exact_only = [
                replace(hit, score=min(hit.score, anchor_score))
                for hit in exact_only[:reserved]
            ]
            ranked = [*semantic_hits, *exact_only]
            ranked.sort(key=lambda hit: hit.score, reverse=True)
            return ranked[: settings.default_top_k]

        if intent == QueryIntent.COMPOUND and semantic_hits:
            # Already fused by rank in ``fuse``; declarations the fusion
            # window dropped follow the fused order instead of outbidding it.
            present = {search_hit_key(hit) for hit in semantic_hits}
            return [
                *semantic_hits,
                *(hit for hit in exact_hits if search_hit_key(hit) not in present),
            ][: settings.default_top_k]

        best: list[SearchHit] = []
        positions: dict[SearchHitKey, int] = {}
        for hit in [*exact_hits, *semantic_hits]:
            key = search_hit_key(hit)
            position = positions.get(key)
            if position is None:
                positions[key] = len(best)
                best.append(hit)
            elif hit.score > best[position].score:
                best[position] = hit
        best.sort(key=lambda hit: hit.score, reverse=True)
        return best[: settings.default_top_k]

    async def merge_path_and_content(
        self,
        state: RetrievalState,
        path_scores: dict[str, float],
        content_hits: list[SearchHit],
        backfill: set[str],
    ) -> list[SearchHit]:
        """Merge path hits into content hits at chunk granularity.

        Deduplicating by path with path hits first would drop the very
        chunk the request wants whenever the path index is right: the
        chunk defining ``add_provider`` inside ``provider.rs`` was discarded
        because the file itself was already represented.
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

        # Only files the content lanes never reached get their first chunk
        # backfilled; that keeps pure filename queries answerable.
        missing = [
            name for name in path_scores if name in backfill and name not in covered
        ]
        if missing:
            try:
                merged.extend(await self.fetch_content_for_paths(missing, path_scores))
            except Exception as exc:
                if not merged:
                    raise
                lane_failed(state, "path_content", exc)

        logger.info(
            "Merged {} content hits with {} path hits (boosted={}, backfilled={})",
            len(content_hits),
            len(path_scores),
            len(covered),
            len(missing),
        )
        merged.sort(key=lambda hit: hit.score, reverse=True)
        return merged

    async def fetch_content_for_paths(
        self,
        blob_names: list[str],
        path_scores: dict[str, float],
    ) -> list[SearchHit]:
        """The first chunk of each file only the path lanes reached."""
        if self.path_content_store is None:
            return []
        hits = await self.path_content_store.get_representative_chunks(blob_names)
        return [replace(hit, score=path_scores.get(hit.blob_name, 0.0)) for hit in hits]
