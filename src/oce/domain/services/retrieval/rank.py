"""The prior and rerank stage: static priors, bounded head slots, model reorder.

Static priors prepare the candidate order; the head rules then give the
first slots to what the request deterministically asked for (the declaration
of a symbol, the named file, a use site of a referenced name, an undemoted
source file for a semantic request) so that a normalized fusion score never
outbids structural evidence. Model rerankers run afterwards on the whole
candidate set and the bounded head is restored on top of their order.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Sequence

from oce.domain.services.query_classifier import (
    QueryIntent,
    asks_about_tests,
    asks_for_implementors,
)
from oce.domain.services.relations import RelationStore
from oce.domain.services.reranker import Reranker
from oce.domain.services.retrieval.hubs import hub_heads, hub_intent
from oce.domain.services.retrieval.names import leaf, word_in
from oce.domain.services.retrieval.priors import (
    is_root_readme,
    neutral_priority_factor,
)
from oce.domain.services.retrieval.state import RetrievalState, lane_failed
from oce.domain.services.retrieval_strategy import plan_rerank
from oce.domain.services.search import (
    ExactSearchStore,
    SearchHit,
    SearchHitKey,
    SearchScope,
    search_hit_key,
)
from oce.domain.services.symbols import HEADER_KINDS
from oce.domain.services.test_paths import is_test_path
from oce.shared.config.settings import RetrievalSettings

# The name a test declaration introduces: ``func TestWalker(``,
# ``def test_walk(``, ``it("walks the tree"``, ``public void walkTest(``.
_TEST_DECLARATION = re.compile(
    r"^\s*(?:@\w+\s+)?(?:pub\s+|async\s+|export\s+|public\s+|static\s+)*"
    r"(?:(?:def|fn|func|function|void|it|test|describe)\s*\(?\s*[\"']?"
    r"(?:\([^)]*\)\s*)?([A-Za-z_][\w ]*))"
)


def names_identifier(path: str, identifiers: Sequence[str]) -> bool:
    """Whether the file is named after one of the identifiers (``foo_test.go`` for ``Foo``)."""
    stem = path.replace("\\", "/").rsplit("/", 1)[-1].split(".", 1)[0]
    normalized = stem.replace("_", "").replace("-", "").lower()
    for identifier in identifiers:
        needle = leaf(identifier).replace("_", "").lower()
        if len(needle) >= 3 and needle in normalized:
            return True
    return False


def test_name_distance(hit: SearchHit, identifiers: Sequence[str]) -> int | None:
    """How far the closest test name declared in the chunk is from the symbol.

    ``TestWalker`` has six extra normalized characters around ``Walk``;
    ``TestWalkInlineMiddlewaresAcrossSubrouter`` has thirty. None when no
    declared test names the symbol.
    """
    needles = [
        leaf(identifier).replace("_", "").lower()
        for identifier in identifiers
        if len(leaf(identifier)) >= 3
    ]
    if not needles:
        return None
    best: int | None = None
    for line in hit.content.splitlines():
        match = _TEST_DECLARATION.match(line)
        if match is None:
            continue
        name = match.group(1).replace("_", "").replace(" ", "").lower()
        for needle in needles:
            if needle in name:
                distance = len(name) - len(needle)
                best = distance if best is None else min(best, distance)
    return best


def path_proximity(path: str, anchors: Sequence[str]) -> int:
    """Longest shared directory prefix (in components) with any anchor path."""
    parts = path.replace("\\", "/").split("/")[:-1]
    best = 0
    for anchor in anchors:
        other = anchor.replace("\\", "/").split("/")[:-1]
        shared = 0
        for left, right in zip(parts, other, strict=False):
            if left != right:
                break
            shared += 1
        best = max(best, shared)
    return best


def promote_heads(
    hits: list[SearchHit], heads: Sequence[SearchHitKey]
) -> list[SearchHit]:
    if not heads:
        return hits
    order = {key: index for index, key in enumerate(heads)}
    tail = len(order)
    return sorted(hits, key=lambda hit: order.get(search_hit_key(hit), tail))


def working_set(
    settings: RetrievalSettings, scope: SearchScope | None
) -> frozenset[str]:
    """Blobs the request just added: the files the user is editing right now.

    A first full sync adds everything, which carries no information, so
    the prior only applies below the configured delta size.
    """
    if scope is None or not scope.added_blob_names:
        return frozenset()
    limit = settings.working_set_boost_max_blobs
    if limit <= 0 or len(scope.added_blob_names) > limit:
        return frozenset()
    if settings.working_set_boost <= 1.0:
        return frozenset()
    return scope.added_blob_names


def apply_source_priority(
    settings: RetrievalSettings,
    hits: list[SearchHit],
    priority_factor: Callable[[str], float],
    boosted: frozenset[str] = frozenset(),
) -> list[SearchHit]:
    """Stable sort by score × path prior × working-set prior."""
    boost = settings.working_set_boost

    def effective(hit: SearchHit) -> float:
        score = hit.score * priority_factor(hit.path)
        if hit.blob_name in boosted:
            score *= boost
        return score

    return sorted(hits, key=effective, reverse=True)


def apply_confidence_floor(
    settings: RetrievalSettings,
    hits: list[SearchHit],
    priority_factor: Callable[[str], float],
    protected: Collection[SearchHitKey] = (),
) -> list[SearchHit]:
    """Drop weak matches by effective score; protected heads always stay."""
    floor = settings.confidence_floor
    return [
        hit
        for hit in hits
        if search_hit_key(hit) in protected
        or hit.score * priority_factor(hit.path) >= floor
    ]


class Ranker:
    def __init__(
        self,
        *,
        settings: RetrievalSettings,
        priority_factor: Callable[[str], float],
        exact_store: ExactSearchStore | None,
        relation_store: RelationStore | None,
        reranker: Reranker | None,
        llm_reranker: Reranker | None,
    ) -> None:
        self.settings = settings
        self.priority_factor = priority_factor
        self.exact_store = exact_store
        self.relation_store = relation_store
        # None means the stage is not authorized; the audit tells that apart
        # from a policy skip.
        self.reranker = reranker
        self.llm_reranker = llm_reranker

    def request_priority_factor(self, state: RetrievalState) -> Callable[[str], float]:
        """Path requests and explicit test questions get a neutral prior.

        A path request may legitimately want a document; a short question
        about tests would otherwise push the very test files it asks for to
        the back. Issue text that merely mentions a file name or a failing
        test is still looking for source, so the prior stays on there.
        """
        if state.strategy.enable_path_index or (
            state.intent != QueryIntent.COMPOUND and asks_about_tests(state.query)
        ):
            return neutral_priority_factor
        return self.priority_factor

    async def rank(self, state: RetrievalState) -> None:
        """Apply static priors, then one candidate-preserving rerank state machine."""
        settings = self.settings
        priority_factor = self.request_priority_factor(state)
        boosted = working_set(settings, state.scope)

        # Static source priors prepare the candidate order. Model rerankers run
        # afterwards, so their returned order cannot be silently overwritten.
        hits = apply_source_priority(
            settings, state.candidates, priority_factor, boosted=boosted
        )
        await self.mark_header_chunks(state, hits)
        hits = await self.prefer_source_head(state, hits, priority_factor)
        structural_heads = self.structural_heads(state, hits, priority_factor)
        if state.audit is not None:
            state.audit.head_slots = len(structural_heads)
        # This optional floor belongs to recall, before model scores can enter
        # the list. Dedicated relevance scores, dense cosine, and RRF are not
        # calibrated to a shared scale; filtering their mixture after
        # reranking is undefined. A deterministic exact symbol/path answer is
        # protected for the same reason.
        hits = apply_confidence_floor(
            settings, hits, priority_factor, protected=structural_heads
        )
        hits = promote_heads(hits, structural_heads)

        decision = plan_rerank(
            state.intent,
            len(hits),
            has_exact_hits=bool(state.exact),
            dense_skipped=bool(
                state.dense_route and state.dense_route.startswith("skip:")
            ),
            # Embedding path similarity is useful recall but not deterministic
            # evidence. Only an exact SQL path/basename match may skip reranking.
            has_path_hits=bool(state.lookup_scores),
            definition_sites=len(state.definitions),
            head_slots=len(structural_heads),
            rerank_ambiguous_definitions=settings.rerank_ambiguous_definitions,
            dedicated_enabled=self.reranker is not None,
            llm_enabled=self.llm_reranker is not None,
            dedicated_policy=settings.rerank_policy,
            llm_policy=settings.llm_rerank_policy,
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
            hits = await self.prefer_source_head(state, hits, priority_factor)
        state.candidates = promote_heads(hits, structural_heads)

    async def mark_header_chunks(
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
            kinds: dict[tuple[str, str], frozenset[str]] = await lookup(
                occurrences, state.scope
            )
        except Exception as exc:
            lane_failed(state, "header_kinds", exc)
            return
        header_kinds = frozenset(HEADER_KINDS)
        state.header_keys = frozenset(
            key for key, seen in kinds.items() if seen and seen <= header_kinds
        )

    async def prefer_source_head(
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
            head = self._test_question_head(state, hits, slots)
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
        head = await self._source_head(state, hits, priority_factor, slots)
        if not head:
            return hits
        head_keys = {search_hit_key(hit) for hit in head}
        return [*head, *(hit for hit in hits if search_hit_key(hit) not in head_keys)]

    @staticmethod
    def _test_question_head(
        state: RetrievalState, hits: list[SearchHit], slots: int
    ) -> list[SearchHit]:
        """ "Which tests cover X": the evidenced use sites inside test files lead.

        Structural evidence decides before the fused order: a chunk that
        declares a test named after the symbol (``TestWalker`` for ``Walk``)
        leads, then chunks that call it, then mentions, then the module
        header that only imports it. Fused order breaks ties between files
        in the same tier; a test file named after the symbol is the one
        written for it.
        """
        identifiers = state.lookup_identifiers
        declaring_paths = tuple(dict.fromkeys(hit.path for hit in state.definitions))
        evidenced = {search_hit_key(hit) for hit in (*state.exact, *state.lexical)}
        head = [
            hit
            for hit in hits
            if search_hit_key(hit) in evidenced and is_test_path(hit.path)
        ]
        use_keys = {search_hit_key(hit) for hit in state.use_sites}
        import_only = {search_hit_key(hit) for hit in state.exact} - use_keys
        first_seen: dict[str, int] = {}
        for hit in head:
            first_seen.setdefault(hit.blob_name, len(first_seen))

        def evidence_tier(hit: SearchHit) -> tuple[int, int]:
            key = search_hit_key(hit)
            distance = test_name_distance(hit, identifiers)
            if distance is not None:
                return (0, distance)
            if key in use_keys:
                return (1, 0)
            return (3 if key in import_only else 2, 0)

        head.sort(
            key=lambda hit: (
                not names_identifier(hit.path, identifiers),
                -path_proximity(hit.path, declaring_paths),
                evidence_tier(hit),
                first_seen[hit.blob_name],
            )
        )
        return head[:slots]

    async def _source_head(
        self,
        state: RetrievalState,
        hits: list[SearchHit],
        priority_factor: Callable[[str], float],
        slots: int,
    ) -> list[SearchHit]:
        """The implementation slots of a semantic or reference request.

        "Where is X used" wants the places that use it: the declaration
        chunk yields the head (a call elsewhere in the declaring file is a
        use like any other). Only exact/lexical occurrence evidence may
        claim a reference head slot; a dense source hit that never names
        the identifier is not a deterministic use site. Within the evidence
        the tiers are structural: chunks that call or extend the symbol,
        then chunks that only mention it textually (uses the extractor
        could not attribute), then chunks that merely import it.
        """
        identifiers = state.lookup_identifiers
        declaring_paths = tuple(dict.fromkeys(hit.path for hit in state.definitions))
        declaring_keys: set[SearchHitKey] = set()
        reference_keys: set[SearchHitKey] | None = None
        use_keys: set[SearchHitKey] = set()
        import_keys: set[SearchHitKey] = set()
        if state.intent == QueryIntent.REFERENCE:
            declaring_keys = {search_hit_key(hit) for hit in state.definitions}
            reference_keys = {
                search_hit_key(hit) for hit in (*state.exact, *state.lexical)
            }
            use_keys = {search_hit_key(hit) for hit in state.use_sites}
            import_keys = (
                {search_hit_key(hit) for hit in state.exact} - use_keys - declaring_keys
            )

        others = list(state.evidence.identifiers[1:] if state.evidence else ())
        implementor_keys = await self.implementor_keys(state, others)

        def eligible(hit: SearchHit) -> bool:
            return (
                # Root README intentionally keeps a neutral multiplicative
                # prior, but it remains documentation and must not consume a
                # slot reserved for implementation code.
                not is_root_readme(hit.path)
                # The declaration chunk yields the head, unless it is also
                # the impl block asked for (a trait and an impl for it often
                # share one chunk).
                and (
                    search_hit_key(hit) not in declaring_keys
                    or search_hit_key(hit) in implementor_keys
                )
                and (reference_keys is None or search_hit_key(hit) in reference_keys)
            )

        def comentions(hit: SearchHit) -> int:
            if not others:
                return 0
            text = f"{hit.context or ''}\n{hit.content}"
            return sum(word_in(name, text) for name in others)

        qualifier_words = tuple(
            dict.fromkeys(q for scopes in state.qualifiers.values() for q in scopes)
        )

        def names_qualifier(hit: SearchHit) -> bool:
            """Whether a chunk names the scope of a qualified request (``app``)."""
            if not qualifier_words:
                return True
            text = f"{hit.context or ''}\n{hit.content}"
            return any(word_in(q, text) for q in qualifier_words)

        def use_tier(hit: SearchHit) -> tuple[bool, int, bool, int, bool, bool, int]:
            """Structural order of reference evidence, most informative first.

            Calls/extensions before textual mentions before imports; a chunk
            that names the qualifier of ``app.render`` before one that only
            says ``render``; among them the chunk that also names the
            request's other symbol ("for ``StatusCode``"); uses in other
            files before uses next to the declaration (the asker knows that
            file); a file named after the symbol before one that is not;
            files closer to the declaring file's package before scripts,
            examples and far-away consumers.
            """
            key = search_hit_key(hit)
            if key in use_keys:
                kind = 0
            elif key in import_keys:
                kind = 2
            else:
                kind = 1
            return (
                key not in implementor_keys,
                kind,
                not names_qualifier(hit),
                -comentions(hit),
                hit.path in declaring_paths,
                not names_identifier(hit.path, identifiers),
                -path_proximity(hit.path, declaring_paths),
            )

        def is_header(hit: SearchHit) -> bool:
            return (
                state.header_keys is not None
                and (hit.blob_name, hit.content_hash) in state.header_keys
            )

        source = [
            hit for hit in hits if eligible(hit) and priority_factor(hit.path) >= 1.0
        ]
        if reference_keys is None:
            head = [hit for hit in source if not is_header(hit)][:slots]
            return head or source[:slots]
        if source or not self.settings.reference_head_fallback:
            source.sort(key=use_tier)
            return source[:slots]
        # Use sites that exist only in tests, examples or package
        # __init__ files are still deterministic use sites; the tiers
        # keep source-like files ahead of test files within the head.
        evidenced_hits = [hit for hit in hits if eligible(hit)]
        evidenced_hits.sort(key=lambda hit: (-priority_factor(hit.path), use_tier(hit)))
        return evidenced_hits[:slots]

    async def implementor_keys(
        self, state: RetrievalState, others: Sequence[str]
    ) -> frozenset[SearchHitKey]:
        """Chunks whose ``inherit`` row names one of ``others`` as the subtype.

        "Where is ``IntoResponse`` implemented for ``StatusCode``": the impl
        block is the inherit occurrence of the trait whose enclosing type is
        the other named symbol, an exact index fact rather than a co-mention.
        """
        store = self.relation_store
        if (
            store is None
            or state.scope is None
            or state.intent != QueryIntent.REFERENCE
            or not others
            or not asks_for_implementors(state.query)
            or not state.lookup_identifiers
        ):
            return frozenset()
        try:
            implementations = await store.find_implementations(
                identifiers=state.lookup_identifiers[:1], scope=state.scope, limit=200
            )
        except Exception as exc:
            lane_failed(state, "implementors", exc)
            return frozenset()
        wanted = {leaf(name) for name in others}
        return frozenset(
            search_hit_key(item.hit)
            for item in implementations
            if item.enclosing in wanted
        )

    def structural_heads(
        self,
        state: RetrievalState,
        hits: Sequence[SearchHit],
        priority_factor: Callable[[str], float],
    ) -> tuple[SearchHitKey, ...]:
        """Bounded deterministic answers protected from score mixing.

        Up to three definitions cover overloads or duplicate declarations. A
        path request may legitimately match several files, so it reserves one
        best chunk per SQL path match up to the final result count. A compound
        request reserves a couple of slots for the definitions of the
        unambiguous identifiers its text names, in source files only.
        """
        settings = self.settings
        in_window = {search_hit_key(hit) for hit in hits}
        if state.intent == QueryIntent.COMPOUND and state.anchors:
            # Anchors keep their own order: the outermost project frame,
            # then the frames it delegated to, then the title's names.
            return tuple(
                search_hit_key(hit)
                for hit in state.anchors
                if search_hit_key(hit) in in_window and priority_factor(hit.path) >= 1.0
            )[: settings.compound_anchor_slots]
        if state.hubs and hub_intent(state):
            return tuple(
                search_hit_key(hit)
                for hit in hub_heads(
                    state.hubs, self.priority_factor, settings.hub_head_slots
                )
                if search_hit_key(hit) in in_window
            )
        if state.intent == QueryIntent.SYMBOL and state.exact:
            # The exact lane already orders declarations: the symbol asked for
            # first, its overloads by the parameter types the request names.
            # Real source still beats the same signature quoted in a
            # documentation code block. Every declaring file gets a slot
            # before a file gets its second (overloads), so two
            # implementations are both visible.
            exact_hits = [
                hit for hit in state.exact if search_hit_key(hit) in in_window
            ]
            ordered = [
                *(hit for hit in exact_hits if priority_factor(hit.path) >= 1.0),
                *(hit for hit in exact_hits if priority_factor(hit.path) < 1.0),
            ]
            slots = min(3, settings.final_select_k)
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
        if state.intent == QueryIntent.CALL_CHAIN and state.endpoints:
            # "How does A reach B": A's declaration is where the reader starts
            # and B's is where the path ends; the hops in between come as a
            # chain section. A one-ended trace ("how does A dispatch...")
            # starts at A just the same. The declarations stay ahead of
            # semantic neighbours, which shuffle between identical requests.
            endpoint_heads: list[SearchHitKey] = []
            for _name, definitions in state.endpoints[:2]:
                for definition in definitions:
                    key = search_hit_key(definition.hit)
                    if key in in_window and key not in endpoint_heads:
                        endpoint_heads.append(key)
                        break
            return tuple(endpoint_heads)
        if state.intent == QueryIntent.PATH and state.lookup_scores:
            path_heads: list[SearchHitKey] = []
            blob_names = sorted(
                state.lookup_scores, key=lambda name: -state.lookup_scores[name]
            )
            for blob_name in blob_names:
                for hit in hits:
                    if hit.blob_name == blob_name:
                        path_heads.append(search_hit_key(hit))
                        break
                if len(path_heads) >= settings.final_select_k:
                    break
            return tuple(path_heads)
        return ()
