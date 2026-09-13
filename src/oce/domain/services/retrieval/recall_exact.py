"""The exact lane: declarations, use sites and anchors of the named symbols.

Everything here is a SQL lookup over ``symbol_occurrences`` keyed by the
identifiers the request spelled. It is the lane that can make a request
decisive on its own: a found declaration answers a symbol question, a found
call site answers a reference question, and no amount of semantic
neighbourhood improves on either.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from oce.domain.services.query_classifier import QueryIntent
from oce.domain.services.retrieval.names import (
    IDENTIFIER_NOISE,
    QUALIFIER_SEPARATORS,
    leaf,
    order_by_comentions,
    order_by_signature_comentions,
    pin_definitions_to_qualifiers,
    resolve_qualified_definitions,
    resolve_qualified_hits,
    word_in,
)
from oce.domain.services.retrieval.state import RetrievalState, lane_failed
from oce.domain.services.search import (
    DefinitionHit,
    ExactSearchStore,
    SearchHit,
    SearchHitKey,
    search_hit_key,
)
from oce.domain.services.symbols import CALL_KIND, DEFINITION_KINDS, USE_SITE_KINDS
from oce.shared.config.settings import RetrievalSettings


async def _no_definitions() -> list[DefinitionHit]:
    return []


def frame_matches(frame_path: str, path: str) -> bool:
    """Whether a traceback frame's file is the indexed file.

    Frame paths are absolute or package-relative; the indexed path is
    repository-relative. One must end with the other, component-aligned, so
    ``requests/sessions.py`` matches ``/site-packages/requests/sessions.py``
    while ``tests/sessions.py`` does not.
    """
    frame = frame_path.replace("\\", "/").strip("/")
    indexed = path.replace("\\", "/").strip("/")
    if not frame or not indexed:
        return False
    if frame == indexed:
        return True
    if frame.endswith("/" + indexed):
        return True
    return indexed.endswith("/" + frame)


def _dedupe(batches: Sequence[Sequence[SearchHit]]) -> list[SearchHit]:
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


class ExactLane:
    def __init__(
        self, store: ExactSearchStore | None, settings: RetrievalSettings
    ) -> None:
        self.store = store
        self.settings = settings

    def available(self, state: RetrievalState) -> bool:
        evidence = state.evidence
        return (
            self.settings.exact_enabled
            and self.store is not None
            and evidence is not None
            and bool(evidence.identifiers)
        )

    async def recall(
        self, state: RetrievalState
    ) -> tuple[list[SearchHit], list[SearchHit], list[SearchHit]]:
        """``(occurrences, definitions, use_sites)`` for the query identifiers.

        Reference questions want every occurrence kind including imports and
        additionally need to know which of those chunks declare the symbol and
        which call or extend it; everything else asks for the structural
        definition only and gets an empty use-site list.
        """
        evidence = state.evidence
        store = self.store
        if (
            not self.settings.exact_enabled
            or store is None
            or state.scope is None
            or not state.scope.blob_names
            or evidence is None
            or not evidence.identifiers
        ):
            return [], [], []
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
            # the combined result would silently discard the bare name. A
            # declaration batch may use the declaration-line evidence; a batch
            # of use sites or mixed occurrences is pinned by structure or
            # text only.
            declarations = kinds is not None and set(kinds) <= set(DEFINITION_KINDS)
            for leaf_name, scopes in state.qualifiers.items():
                if identifier == leaf_name or any(
                    identifier.endswith(f"{separator}{leaf_name}")
                    for separator in QUALIFIER_SEPARATORS
                ):
                    return resolve_qualified_hits(
                        hits, {leaf_name: scopes}, declarations=declarations
                    )
            return hits

        async def lookup(kinds: Sequence[str] | None) -> list[SearchHit]:
            if not state.qualifiers:
                return await store.search_exact(
                    identifiers=identifiers, scope=scope, top_k=top_k, kinds=kinds
                )
            batches = await asyncio.gather(
                *(lookup_one(identifier, kinds) for identifier in identifiers)
            )
            return _dedupe(batches)

        use_sites: list[SearchHit] = []
        try:
            with state.stage("exact"):
                if state.intent == QueryIntent.REFERENCE:
                    occurrences, definitions, use_sites = await asyncio.gather(
                        lookup(None),
                        lookup(DEFINITION_KINDS),
                        lookup_one(leaf(evidence.identifiers[0]), USE_SITE_KINDS),
                    )
                elif state.intent == QueryIntent.CALL_CHAIN:
                    occurrences, definitions, state.endpoints = await asyncio.gather(
                        lookup((*DEFINITION_KINDS, CALL_KIND)),
                        lookup(DEFINITION_KINDS),
                        self.resolve_endpoints(state, identifiers),
                    )
                elif state.intent == QueryIntent.SYMBOL and len(identifiers) > 1:
                    # "Where is ``fromJson(JsonReader, TypeToken)`` defined":
                    # the first name is the symbol asked for, the others pick
                    # its overload. Its declarations lead in that order; the
                    # damping by declaration count must not let a type named
                    # once outrank the overloaded method asked about.
                    first_leaf = leaf(identifiers[0])
                    batch_list, overloads = await asyncio.gather(
                        asyncio.gather(
                            *(
                                lookup_one(identifier, DEFINITION_KINDS)
                                for identifier in identifiers
                            )
                        ),
                        store.find_definitions(
                            identifiers=(first_leaf,),
                            scope=scope,
                            max_per_identifier=40,
                        ),
                    )
                    batches = list(batch_list)
                    others = [
                        *evidence.identifiers[1:],
                        *(
                            scope_name
                            for scopes in state.qualifiers.values()
                            for scope_name in scopes
                        ),
                    ]
                    batches[0] = order_by_signature_comentions(
                        batches[0], overloads, others
                    )
                    primary_leaf = leaf(evidence.identifiers[0])
                    state.primary_definition_found = any(
                        batch
                        for identifier, batch in zip(identifiers, batches, strict=True)
                        if leaf(identifier) == primary_leaf
                    )
                    definitions = _dedupe(batches)
                    occurrences = definitions
                else:
                    definitions = await lookup(DEFINITION_KINDS)
                    occurrences = definitions
                    if state.intent == QueryIntent.SYMBOL:
                        state.primary_definition_found = bool(definitions)
        except Exception as exc:
            lane_failed(state, "exact", exc)
            return [], [], []
        # A qualified request (``Session.get``) pins the leaf to a scope; the
        # filtering was applied to that identifier's batch above. Among the
        # remaining declarations, the ones that mention the request's other
        # names (parameter types of an overload) come first.
        if state.intent == QueryIntent.SYMBOL and len(identifiers) == 1:
            # The request's other names: the scopes of a qualified name. The
            # looked-up name itself is in every hit.
            others = [scope for scopes in state.qualifiers.values() for scope in scopes]
            occurrences = order_by_comentions(occurrences, others)
            definitions = order_by_comentions(definitions, others)
        elif state.intent == QueryIntent.REFERENCE and len(evidence.identifiers) > 1:
            # "Where is ``IntoResponse`` implemented for ``StatusCode``": the
            # use site that names the other symbol too is the one asked for.
            others = list(evidence.identifiers[1:])
            occurrences = order_by_comentions(occurrences, others)
            use_sites = order_by_comentions(use_sites, others)
        if state.audit is not None:
            state.audit.exact_definitions = len(definitions)
            state.audit.definition_sites = len(definitions)
        return occurrences, definitions, use_sites

    async def resolve_endpoints(
        self, state: RetrievalState, identifiers: Sequence[str]
    ) -> list[tuple[str, list[DefinitionHit]]]:
        """Declarations of each queried name, qualified names pinned to their scope.

        Only names that resolve to a bounded number of declarations count as
        endpoints; ``send`` declared in twenty classes anchors nothing unless
        the request qualified it.
        """
        store = self.store
        if store is None or state.scope is None:
            return []
        # Qualified spellings are kept for the symbol table; the leaf is what
        # the declaration rows carry.
        leaves = list(dict.fromkeys(leaf(identifier) for identifier in identifiers))
        if not leaves:
            return []
        # A qualified name may be declared in many types (``route`` on every
        # router); the qualifier picks one afterwards, so the bound is wide
        # for those and tight for bare names.
        qualified = [name for name in leaves if name in state.qualifiers]
        bare = [name for name in leaves if name not in state.qualifiers]
        # A qualifier that is the recorded enclosing definition (``Router``
        # for ``Router::route``) pins the leaf in SQL, so ``route`` declared
        # in fifty routers is no obstacle; qualifiers that are only a file or
        # a text mention fall back to the wide lookup resolved below.
        pinned_batches = await asyncio.gather(
            *(
                store.find_definitions(
                    identifiers=(name,),
                    scope=state.scope,
                    max_per_identifier=6,
                    enclosing=state.qualifiers[name],
                )
                for name in qualified
            )
        )
        pinned = {
            name: batch
            for name, batch in zip(qualified, pinned_batches, strict=True)
            if batch
        }
        unpinned = [name for name in qualified if name not in pinned]
        batches = await asyncio.gather(
            *(
                store.find_definitions(
                    identifiers=group, scope=state.scope, max_per_identifier=bound
                )
                for group, bound in ((unpinned, 40), (bare, 6))
                if group
            )
        )
        definitions = [
            *(item for batch in pinned.values() for item in batch),
            *(item for batch in batches for item in batch),
        ]
        endpoints: list[tuple[str, list[DefinitionHit]]] = []
        for name in leaves:
            found = [item for item in definitions if item.identifier == name]
            scopes = state.qualifiers.get(name)
            if scopes and found:
                found = resolve_qualified_definitions(found, {name: scopes})
            # Endpoints form a prefix of the names in the question. If the
            # start cannot be resolved, a later target must not become the
            # start of a reversed one-ended trace.
            if not found:
                break
            endpoints.append((name, found))
        return endpoints

    async def recall_anchors(self, state: RetrievalState) -> list[SearchHit]:
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
        store = self.store
        if (
            state.intent != QueryIntent.COMPOUND
            or self.settings.compound_anchor_slots <= 0
            or not self.settings.exact_enabled
            or store is None
            or state.scope is None
            or not state.scope.blob_names
            or evidence is None
            or not (evidence.frames or evidence.identifiers)
        ):
            return []
        scope = state.scope
        title = state.query.strip().splitlines()[0] if state.query.strip() else ""
        title_identifiers = [
            identifier
            for identifier in state.lookup_identifiers
            if word_in(leaf(identifier), title)
            and leaf(identifier).lower() not in IDENTIFIER_NOISE
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
            lane_failed(state, "anchors", exc)
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
                and frame_matches(frame.path, definition.hit.path)
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
        title_definitions = pin_definitions_to_qualifiers(
            title_definitions, state.qualifiers, strict=True
        )
        for definition in title_definitions:
            key = search_hit_key(definition.hit)
            if key not in seen:
                seen.add(key)
                anchors.append(definition.hit)
        return anchors
