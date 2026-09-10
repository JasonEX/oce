"""Pure ranking of candidates and bounded, structurally evidenced answer slots."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from oce.domain.chunk.lang import detect_language
from oce.domain.services.query_classifier import QueryIntent, QueryRoute
from oce.domain.services.search import (
    DefinitionHit,
    SearchHit,
    SearchHitKey,
    search_hit_key,
)
from oce.domain.services.symbol_resolution import _leaf, _word_in
from oce.domain.services.test_paths import is_test_path
from oce.shared.config.settings import RetrievalSettings


@dataclass(frozen=True)
class HeadEvidence:
    """Facts needed to order use sites; contains no I/O or retrieval lifecycle state."""

    route: QueryRoute
    identifiers: tuple[str, ...] = ()
    qualifiers: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    definitions: Sequence[SearchHit] = ()
    exact: Sequence[SearchHit] = ()
    use_sites: Sequence[SearchHit] = ()
    lexical: Sequence[SearchHit] = ()
    header_keys: frozenset[tuple[str, str]] = frozenset()
    implementor_keys: frozenset[SearchHitKey] = frozenset()


def source_priority_factor(path: str) -> float:
    """源码优先先验：文档/测试类路径乘性降权，普通源码 1.0。"""
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
    # Vendored third-party code is real source the project does not own; a
    # request about the project is looking for the code that calls into it.
    if any(f"/{part}/" in f"/{p}" for part in _VENDORED_DIRECTORIES):
        return 0.6
    # 配置文件和类型桩：需要它们的查询会写出文件名（PATH 意图，中立先验），
    # 其余查询在找实现，这些文件只是碰巧提到同样的名字。
    if (
        name.endswith(_CONFIG_SUFFIXES)
        or _RC_FILE.match(name)
        or name.endswith(".pyi")
        or ".config." in name
    ):
        return 0.7
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


_VENDORED_DIRECTORIES = frozenset(
    {"vendor", "vendored", "_vendor", "third_party", "thirdparty", "node_modules"}
)


_DOCUMENT_STEMS = frozenset(
    {"changelog", "changes", "history", "news", "authors", "contributors", "todo"}
)


_PRIMARY_README_NAMES = frozenset(
    {"readme", "readme.md", "readme.rst", "readme.txt", "readme.adoc"}
)


_CONFIG_SUFFIXES = (".cfg", ".ini", ".toml", ".yaml", ".yml", ".json")


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


def _names_identifier(path: str, identifiers: Sequence[str]) -> bool:
    """Whether the file is named after one of the identifiers (``foo_test.go`` for ``Foo``)."""
    stem = path.replace("\\", "/").rsplit("/", 1)[-1].split(".", 1)[0]
    normalized = stem.replace("_", "").replace("-", "").lower()
    for identifier in identifiers:
        leaf = identifier.rsplit("::", 1)[-1].rsplit(".", 1)[-1]
        needle = leaf.replace("_", "").lower()
        if len(needle) >= 3 and needle in normalized:
            return True
    return False


_TEST_DECLARATION = re.compile(
    r"^\s*(?:@\w+\s+)?(?:pub\s+|async\s+|export\s+|public\s+|static\s+)*"
    r"(?:(?:def|fn|func|function|void|it|test|describe)\s*\(?\s*[\"']?"
    r"(?:\([^)]*\)\s*)?([A-Za-z_][\w ]*))"
)

_QUOTED_TEST_DECLARATION = re.compile(
    r"^\s*(?:it|test|describe)\s*\(\s*"
    r"(?:'((?:\\.|[^'\\])*)'|\"((?:\\.|[^\"\\])*)\")"
)


def _test_name_distance(hit: SearchHit, identifiers: Sequence[str]) -> int | None:
    """How far the closest test name declared in the chunk is from the symbol.

    ``TestWalker`` has six extra normalized characters around ``Walk``;
    ``TestWalkInlineMiddlewaresAcrossSubrouter`` has thirty. None when no
    declared test names the symbol.
    """
    needles = [
        _leaf(identifier).replace("_", "").lower()
        for identifier in identifiers
        if len(_leaf(identifier)) >= 3
    ]
    if not needles:
        return None
    best: int | None = None
    for line in hit.content.splitlines():
        quoted = _QUOTED_TEST_DECLARATION.match(line)
        if quoted:
            declared = quoted.group(1) or quoted.group(2)
        else:
            match = _TEST_DECLARATION.match(line)
            if match is None:
                continue
            declared = match.group(1)
        # Quoted test titles may contain punctuation. Read the full title;
        # stopping at a hyphen makes every "symbol - scenario" tie.
        name = re.sub(r"[^A-Za-z0-9]", "", declared).lower()
        for needle in needles:
            if needle in name:
                distance = len(name) - len(needle)
                best = distance if best is None else min(best, distance)
    return best


def _path_proximity(path: str, anchors: Sequence[str]) -> int:
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


def source_heads(
    evidence: HeadEvidence,
    hits: list[SearchHit],
    priority_factor: Callable[[str], float],
    *,
    slots: int,
    reference_fallback: bool,
) -> tuple[SearchHitKey, ...]:
    """Give the first slots to undemoted source files, in their own order.

    A test or documentation chunk that leads both the dense and the
    lexical list keeps a normalized RRF score no multiplicative prior can
    undercut, yet the request almost never asks for it first. Reference
    questions additionally keep the symbol's own declaration out of those
    slots: the question is where it is used. The demoted hits are not
    dropped; they follow immediately after the reserved slots.
    """
    declaring_paths = tuple(dict.fromkeys(hit.path for hit in evidence.definitions))
    identifiers = evidence.identifiers
    if slots > 0 and evidence.route.overrides_requested:
        # An override is a declaration of the requested method. Call sites
        # cannot answer this request merely because they invoke the method.
        definitions = {search_hit_key(hit) for hit in evidence.definitions}
        return tuple(
            search_hit_key(hit) for hit in hits if search_hit_key(hit) in definitions
        )[:slots]
    if (
        slots > 0
        and evidence.route.intent == QueryIntent.REFERENCE
        and evidence.route.tests_requested
    ):
        # "Which tests cover X": the evidenced use sites inside test files
        # are the answer, so they take the head instead of yielding it. A
        # test file named after the symbol is the one written for it.
        evidenced = {
            search_hit_key(hit) for hit in (*evidence.exact, *evidence.lexical)
        }
        head = [
            hit
            for hit in hits
            if search_hit_key(hit) in evidenced and is_test_path(hit.path)
        ]
        # Structural evidence decides before the fused order: a chunk
        # that declares a test named after the symbol (``TestWalker`` for
        # ``Walk``) leads, then chunks that call it, then mentions, then
        # the module header that only imports it. Fused order breaks ties
        # between files in the same tier.
        use_keys = {search_hit_key(hit) for hit in evidence.use_sites}
        import_only = {search_hit_key(hit) for hit in evidence.exact} - use_keys
        first_seen: dict[str, int] = {}
        for hit in head:
            first_seen.setdefault(hit.blob_name, len(first_seen))

        def evidence_tier(hit: SearchHit) -> tuple[int, int]:
            key = search_hit_key(hit)
            distance = _test_name_distance(hit, identifiers)
            if distance is not None:
                return (0, distance)
            if key in use_keys:
                return (1, 0)
            return (3 if key in import_only else 2, 0)

        head.sort(
            key=lambda hit: (
                evidence_tier(hit),
                not _names_identifier(hit.path, identifiers),
                -_path_proximity(hit.path, declaring_paths),
                first_seen[hit.blob_name],
            )
        )
        head = head[:slots]
        if head:
            return tuple(search_hit_key(hit) for hit in head)
    # Symbol/path answers have their own structural heads. Broad semantic
    # requests, including overviews, reserve a few implementation slots;
    # documentation remains in the tail and coverage selection can retain it.
    if (
        slots <= 0
        or priority_factor is neutral_priority_factor
        or evidence.route.intent in (QueryIntent.SYMBOL, QueryIntent.PATH)
    ):
        return ()
    # "Where is X used" wants the places that use it: the declaration
    # chunk yields the head (a call elsewhere in the declaring file is a
    # use like any other). Only exact/lexical occurrence evidence may
    # claim a reference head slot; a dense source hit that never names
    # the identifier is not a deterministic use site. Within the evidence
    # the tiers are structural: chunks that call or extend the symbol,
    # then chunks that only mention it textually (uses the extractor
    # could not attribute), then chunks that merely import it.
    declaring_keys: set[SearchHitKey] = set()
    reference_keys: set[SearchHitKey] | None = None
    use_keys: set[SearchHitKey] = set()
    import_keys: set[SearchHitKey] = set()
    if evidence.route.intent == QueryIntent.REFERENCE:
        declaring_keys = {search_hit_key(hit) for hit in evidence.definitions}
        reference_keys = {
            search_hit_key(hit) for hit in (*evidence.exact, *evidence.lexical)
        } | set(evidence.implementor_keys)
        use_keys = {search_hit_key(hit) for hit in evidence.use_sites}
        import_keys = (
            {search_hit_key(hit) for hit in evidence.exact} - use_keys - declaring_keys
        )

    others = list(evidence.identifiers[1:])
    implementor_keys = evidence.implementor_keys

    def eligible(hit: SearchHit) -> bool:
        return (
            # Root README intentionally keeps a neutral multiplicative
            # prior, but it remains documentation and must not consume a
            # slot reserved for implementation code.
            not _is_root_readme(hit.path)
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
        return sum(_word_in(name, text) for name in others)

    qualifier_words = tuple(
        dict.fromkeys(q for scopes in evidence.qualifiers.values() for q in scopes)
    )

    def names_qualifier(hit: SearchHit) -> bool:
        """Whether a chunk names the scope of a qualified request (``app``)."""
        if not qualifier_words:
            return True
        text = f"{hit.context or ''}\n{hit.content}"
        return any(_word_in(q, text) for q in qualifier_words)

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
            # A call in another file is the clearest additional use. Local
            # calls share the next tier with textual uses, where file
            # diversity can expose consumers of a type's static methods.
            kind = int(hit.path in declaring_paths)
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
            not _names_identifier(hit.path, identifiers),
            -_path_proximity(hit.path, declaring_paths),
        )

    def is_header(hit: SearchHit) -> bool:
        return (
            evidence.header_keys is not None
            and (hit.blob_name, hit.content_hash) in evidence.header_keys
        )

    source = [hit for hit in hits if eligible(hit) and priority_factor(hit.path) >= 1.0]
    head: list[SearchHit] = []
    if reference_keys is None:
        head = [hit for hit in source if not is_header(hit)][:slots]
        if not head:
            head = source[:slots]
    else:
        source.sort(key=use_tier)
        head = source[:slots]
        if reference_fallback and len(head) < slots:
            # A single source caller cannot fill a coverage request. Fill the
            # remaining slots with evidenced uses in tests/examples before
            # admitting semantic neighbours that never use the name.
            chosen = {search_hit_key(hit) for hit in head}
            remaining = [
                hit
                for hit in hits
                if eligible(hit) and search_hit_key(hit) not in chosen
            ]
            remaining.sort(key=lambda hit: (-priority_factor(hit.path), use_tier(hit)))
            head.extend(remaining[: slots - len(head)])
    if not head:
        return ()
    return tuple(search_hit_key(hit) for hit in head)


def structural_heads(
    hits: Sequence[SearchHit],
    *,
    intent: QueryIntent,
    exact: Sequence[SearchHit],
    anchors: Sequence[SearchHit],
    endpoints: Sequence[tuple[str, list[DefinitionHit]]],
    lookup_scores: Mapping[str, float],
    settings: RetrievalSettings,
    priority_factor: Callable[[str], float],
) -> tuple[SearchHitKey, ...]:
    """Bounded deterministic answers protected from score mixing.

    Up to three definitions cover overloads or duplicate declarations. A
    path request may legitimately match several files, so it reserves one
    best chunk per SQL path match up to the final result count. A compound
    request reserves a couple of slots for the definitions of the
    unambiguous identifiers its text names, in source files only.
    """
    if intent == QueryIntent.COMPOUND and anchors:
        # Anchors keep their own order: the outermost project frame,
        # then the frames it delegated to, then the title's names.
        factor = priority_factor
        in_window = {search_hit_key(hit) for hit in hits}
        return tuple(
            search_hit_key(hit)
            for hit in anchors
            if search_hit_key(hit) in in_window and factor(hit.path) >= 1.0
        )[: settings.compound_anchor_slots]
    if intent == QueryIntent.SYMBOL and exact:
        # The exact lane already orders declarations: the symbol asked for
        # first, its overloads by the parameter types the request names.
        # Real source still beats the same signature quoted in a
        # documentation code block. Every declaring file gets a slot
        # before a file gets its second (overloads), so two
        # implementations are both visible.
        factor = priority_factor
        in_window = {search_hit_key(hit) for hit in hits}
        exact_hits = [hit for hit in exact if search_hit_key(hit) in in_window]
        ordered = [
            *(hit for hit in exact_hits if factor(hit.path) >= 1.0),
            *(hit for hit in exact_hits if factor(hit.path) < 1.0),
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
    if intent == QueryIntent.CALL_CHAIN and endpoints:
        # "How does A reach B": A's declaration is where the reader starts
        # and B's is where the path ends; the hops in between come as a
        # chain section. A one-ended trace ("how does A dispatch...")
        # starts at A just the same. The declarations stay ahead of
        # semantic neighbours, which shuffle between identical requests.
        in_window = {search_hit_key(hit) for hit in hits}
        heads: list[SearchHitKey] = []
        for _leaf, definitions in endpoints[:2]:
            for definition in definitions:
                key = search_hit_key(definition.hit)
                if key in in_window and key not in heads:
                    heads.append(key)
                    break
        return tuple(heads)
    if intent == QueryIntent.PATH and lookup_scores:
        heads: list[SearchHitKey] = []
        blob_names = sorted(
            lookup_scores,
            key=lambda name: -lookup_scores[name],
        )
        for blob_name in blob_names:
            for hit in hits:
                if hit.blob_name == blob_name:
                    heads.append(search_hit_key(hit))
                    break
            if len(heads) >= settings.final_select_k:
                break
        return tuple(heads)
    return ()


def promote_heads(
    hits: list[SearchHit],
    heads: Sequence[SearchHitKey],
) -> list[SearchHit]:
    if not heads:
        return hits
    order = {key: index for index, key in enumerate(heads)}
    tail = len(order)
    return sorted(hits, key=lambda hit: order.get(search_hit_key(hit), tail))


def apply_priors(
    hits: list[SearchHit],
    *,
    priority_factor: Callable[[str], float],
    boosted: frozenset[str],
    working_set_boost: float,
) -> list[SearchHit]:
    """Apply the source and working-set priors once, preserving original scores."""
    factor = priority_factor
    boost = working_set_boost

    def effective(hit: SearchHit) -> float:
        score = hit.score * factor(hit.path)
        if hit.blob_name in boosted:
            score *= boost
        return score

    return sorted(hits, key=effective, reverse=True)
