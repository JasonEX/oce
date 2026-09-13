"""The hub lane: entry-point declarations spelled by a request's words.

"Explain the request context lifecycle" names no symbol, yet the scope
declares ``RequestContext`` and ``request_context``, and the files that
declare them are where the answer starts. The lane generates the spellings a
declaration could use from the request's content words, looks them up with
their reference fan-in, and hands the most widely referenced ones to the
head rules. It is measured, not assumed: on the curated overview set it
raised nDCG@10 67.8 to 74.3, on the sealed held-out semantic set it lowered
overview 66.4 to 54.1 and call-chain 85.6 to 78.2, so ``hub_head_slots``
ships at 0. Extending the lane to feature questions was measured once and
retired: a feature question describes one behaviour, and its answer is the
function that implements it rather than the type at the centre of the
subsystem (semantic feature nDCG@10 73.5 to 68.8, CSN Region Top-1 62.5 to
60.0; see benchmarks/results/utility-round3-2026-09-09.md).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

from oce.domain.services.lexical import STOPWORDS
from oce.domain.services.lexical import split_identifier as _split_words
from oce.domain.services.query_classifier import QueryIntent
from oce.domain.services.retrieval.names import IDENTIFIER_NOISE
from oce.domain.services.retrieval.state import RetrievalState, lane_failed
from oce.domain.services.search import ExactSearchStore, HubDefinition, SearchHit
from oce.shared.config.settings import RetrievalSettings

_QUERY_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]*")
_HUB_MAX_WORDS = 14


def _word_stems(word: str) -> tuple[str, ...]:
    """Spellings a declared name may use for an English word in the request.

    Plural and inflected forms are the common gap ("registers a checker" ->
    ``register_checker``); the candidates are generated, not looked up, so
    a wrong stem costs nothing unless a declaration happens to spell it.
    """
    stems = [word]
    if word.endswith("ies") and len(word) > 4:
        stems.append(word[:-3] + "y")
    if word.endswith(("ches", "shes", "sses", "xes")) and len(word) > 5:
        stems.append(word[:-2])
    elif word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        stems.append(word[:-1])
    if word.endswith("ing") and len(word) > 5:
        stems.append(word[:-3])
        stems.append(word[:-3] + "e")
    if word.endswith("ed") and len(word) > 4:
        stems.append(word[:-2])
        stems.append(word[:-1])
    return tuple(dict.fromkeys(stem for stem in stems if len(stem) >= 3))


def hub_spellings(
    query: str, mentions: Sequence[str] = (), identifiers: Sequence[str] = ()
) -> tuple[str, ...]:
    """Identifier spellings the request's words could declare.

    Every content word alone (``Router``, ``router``) and every pair of
    content words joined as snake, camel or Pascal case in either order
    (``register_checker``, ``createSlice``, ``JsonReader``), plus the exact
    type names and code identifiers the request already carries. The
    request is not a symbol query, so the words are ordinary English and
    the pairs are bounded by the number of content words.
    """
    words: list[str] = []
    for match in _QUERY_WORD.finditer(query):
        word = match.group().lower()
        if word in STOPWORDS or len(word) < 2 or word in words:
            continue
        words.append(word)
    words = words[:_HUB_MAX_WORDS]
    stems = [_word_stems(word) for word in words]
    spellings: list[str] = list(dict.fromkeys((*identifiers, *mentions)))
    for options in stems:
        for stem in options:
            if len(stem) >= 3:
                spellings.extend((stem, stem.capitalize()))
    for left_index, left in enumerate(stems):
        for right in stems[left_index + 1 :]:
            for first, second in ((left, right), (right, left)):
                for a in first:
                    for b in second:
                        spellings.append(f"{a}_{b}")
                        spellings.append(f"{a}{b.capitalize()}")
                        spellings.append(f"{a.capitalize()}{b.capitalize()}")
    return tuple(dict.fromkeys(spellings))


def hub_intent(state: RetrievalState) -> bool:
    """Requests answered by the code's entry points rather than a named symbol.

    Overviews and flow questions that name no symbol are; a feature question
    is not (see the module docstring).
    """
    if state.intent == QueryIntent.OVERVIEW:
        return True
    if state.intent == QueryIntent.CALL_CHAIN:
        return not state.lookup_identifiers
    return False


async def recall_hubs(
    state: RetrievalState,
    settings: RetrievalSettings,
    exact_store: ExactSearchStore | None,
) -> list[HubDefinition]:
    """Declared names the request's words spell, by reference fan-in.

    The store returns the spellings that are declared in a bounded number
    of places together with how many scoped files reference each. Names
    that are also a package directory (``routing``, ``_pytest``) are
    reported but never lead: their references belong to the package, not
    to the declaration.
    """
    evidence = state.evidence
    lookup = getattr(exact_store, "find_hub_definitions", None)
    if (
        not hub_intent(state)
        or settings.hub_head_slots <= 0
        or not settings.exact_enabled
        or lookup is None
        or state.scope is None
        or not state.scope.blob_names
        or evidence is None
    ):
        return []
    spellings = hub_spellings(state.query, evidence.mentions, state.lookup_identifiers)
    if not spellings:
        return []
    try:
        with state.stage("exact"):
            hubs: list[HubDefinition] = await lookup(
                spellings=spellings,
                scope=state.scope,
                max_per_identifier=settings.hub_max_definitions,
            )
    except Exception as exc:
        lane_failed(state, "hubs", exc)
        return []
    mentioned = set(evidence.mentions) | set(state.lookup_identifiers)

    def specificity(hub: HubDefinition) -> int:
        """Words of the request a spelling accounts for."""
        if hub.identifier in mentioned:
            return 3
        parts = [part for part in _split_words(hub.identifier) if len(part) >= 2]
        return min(len(parts), 2)

    kept = [
        hub
        for hub in hubs
        if hub.identifier.lower() not in IDENTIFIER_NOISE
        and hub.identifier.lower() not in STOPWORDS
    ]
    # A spelling that accounts for more of the request's words is the
    # more specific match (``register_checker`` over ``Checker``); among
    # equally specific names the more widely referenced one leads.
    kept.sort(key=lambda hub: (-specificity(hub), -hub.referencing_files))
    return kept


def hub_heads(
    hubs: Sequence[HubDefinition],
    priority_factor: Callable[[str], float],
    slots: int,
) -> list[SearchHit]:
    """The declaration chunk of each leading hub, source files only.

    A hub must be referenced from more than one scoped file (a name
    used by a single file is that file's helper) and must not be a
    package name; among its declarations the largest one is the
    implementation (a class over an enum member or a one-line alias).
    Two hubs never share a file, so two slots show two entry points.
    """
    heads: list[SearchHit] = []
    seen_blobs: set[str] = set()
    for hub in hubs:
        if hub.names_package or hub.referencing_files < 2:
            continue
        candidates = sorted(
            (
                definition
                for definition in hub.definitions
                if priority_factor(definition.hit.path) >= 1.0
                and definition.hit.blob_name not in seen_blobs
            ),
            key=lambda item: -(item.end_line - item.start_line),
        )
        if not candidates:
            continue
        seen_blobs.add(candidates[0].hit.blob_name)
        heads.append(candidates[0].hit)
        if len(heads) >= slots:
            break
    return heads
