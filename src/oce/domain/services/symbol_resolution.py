"""Pure resolution of qualified names and declaration signatures."""

from __future__ import annotations

import re
from collections.abc import Sequence

from oce.domain.services.search import (
    DefinitionHit,
    SearchHit,
    SearchHitKey,
    search_hit_key,
)


def _frame_matches(frame_path: str, path: str) -> bool:
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


_QUALIFIER_SEPARATORS = ("::", ".")


def split_qualified_identifiers(
    identifiers: Sequence[str],
) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]]]:
    """``Session.get`` -> look up ``get`` and remember that it must sit in ``Session``.

    The qualified spelling is kept as well: a symbol table may hold dotted
    names verbatim. The qualifier map only records real scopes, so a bare
    identifier contributes nothing to it.
    """
    lookup: list[str] = []
    qualifiers: dict[str, list[str]] = {}
    for identifier in identifiers:
        if identifier not in lookup:
            lookup.append(identifier)
        for separator in _QUALIFIER_SEPARATORS:
            if separator in identifier:
                scope, leaf = identifier.rsplit(separator, 1)
                scope_leaf = scope.rsplit(separator, 1)[-1]
                if leaf and scope_leaf:
                    if leaf not in lookup:
                        lookup.append(leaf)
                    bucket = qualifiers.setdefault(leaf, [])
                    if scope_leaf not in bucket:
                        bucket.append(scope_leaf)
                break
    return tuple(lookup), {leaf: tuple(scopes) for leaf, scopes in qualifiers.items()}


def _leaf(identifier: str) -> str:
    """``Session.get`` / ``Router::route`` -> ``get`` / ``route``."""
    for separator in _QUALIFIER_SEPARATORS:
        if separator in identifier:
            identifier = identifier.rsplit(separator, 1)[-1]
    return identifier


def _word_in(word: str, text: str) -> bool:
    return (
        re.search(rf"(?<![A-Za-z0-9_$]){re.escape(word)}(?![A-Za-z0-9_$])", text)
        is not None
    )


def mentions_requested_name(hit: SearchHit, name: str) -> bool:
    """A complete spelling or a leaf inside its enclosing or module scope.

    Mere co-mention of a qualifier and leaf in prose is too weak to admit a
    dense neighbour as a use of a qualified name.
    """
    if _word_in(name, f"{hit.context or ''}\n{hit.content}"):
        return True
    _, qualifiers = split_qualified_identifiers((name,))
    return (
        bool(qualifiers)
        and _word_in(_leaf(name), hit.content)
        and any(
            _word_in(scope, hit.context or "") or scope in _path_components(hit.path)
            for scopes in qualifiers.values()
            for scope in scopes
        )
    )


_DECLARATION_LINE = re.compile(
    r"\b(?:def|fn|func|function|class|struct|impl|interface|trait|enum|type"
    r"|val|var|let|const|public|private|protected|static|override|sub|proc)\b"
    r"|::"
)


def _path_components(path: str) -> tuple[str, ...]:
    """Directory names plus the file stem: ``binding/default.go`` -> ``binding``, ``default``."""
    parts = path.replace("\\", "/").split("/")
    name = parts[-1]
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return (*parts[:-1], stem)


def resolve_qualified_hits(
    hits: list[SearchHit],
    qualifiers: dict[str, tuple[str, ...]],
    *,
    declarations: bool = True,
    strict: bool = False,
) -> list[SearchHit]:
    """Keep the hits that belong to the requested scope.

    For declarations the scope chain (``class Session > def get``) and a
    path component (``binding/default.go``) are the structural evidence; a
    declaration line naming both the scope and the leaf (``app.render =
    function render``, ``func (c *Context) ShouldBindJSON``) comes next; the
    chunk text is consulted last (Go receivers, C++ ``Type::method``
    definitions in chunks without a scope chain). Use sites are pinned by
    structure or text only: the line that calls ``app.render`` is not a
    declaration. A path component must equal the qualifier whole, so a test
    file named ``app.render.js`` does not pass for the scope ``app``. When
    no hit matches, the request may have named a scope the index does not
    know, so every hit stays unless ``strict`` asks for nothing instead.
    """
    if not qualifiers or not hits:
        return hits
    unresolved: list[SearchHit] = [] if strict else hits
    wanted = tuple(dict.fromkeys(q for scopes in qualifiers.values() for q in scopes))
    leaves = tuple(qualifiers)

    def structural(hit: SearchHit) -> bool:
        if any(_word_in(q, hit.context or "") for q in wanted):
            return True
        components = _path_components(hit.path)
        return any(q in components for q in wanted)

    def declared(hit: SearchHit) -> bool:
        for line in hit.content.splitlines():
            if (
                _DECLARATION_LINE.search(line)
                and any(_word_in(leaf, line) for leaf in leaves)
                and any(_word_in(q, line) for q in wanted)
            ):
                return True
        return False

    def textual(hit: SearchHit) -> bool:
        if not declarations:
            # The SQL batch already proves the leaf is an occurrence. A scope
            # mention can identify an instance's owner even when a call uses
            # the local instance name rather than its declared type.
            return any(_word_in(q, hit.content) for q in wanted)
        return any(
            _word_in(f"{q}{separator}{leaf}", hit.content)
            for q in wanted
            for leaf in leaves
            for separator in _QUALIFIER_SEPARATORS
        )

    if not declarations:
        # A call line ``app.render(...)`` in one file and a chunk whose scope
        # chain names ``app`` in another are both use sites; neither kind of
        # evidence suppresses the other.
        matched = [hit for hit in hits if structural(hit) or textual(hit)]
        return matched or unresolved
    for predicate in (structural, declared, textual):
        matched = [hit for hit in hits if predicate(hit)]
        if matched:
            return matched
    return unresolved


def resolve_qualified_definitions(
    definitions: list[DefinitionHit],
    qualifiers: dict[str, tuple[str, ...]],
    *,
    strict: bool = False,
) -> list[DefinitionHit]:
    """Declarations of the leaf inside the requested scope.

    The recorded enclosing definition is the index fact (``route`` declared
    inside ``Router``); the chunk-level evidence is the fallback for stores
    that did not record one. ``strict`` returns nothing when no evidence
    places any declaration in the scope (``unittest.skip`` names the
    standard library's ``skip``, not the project's).
    """
    wanted = {q for scopes in qualifiers.values() for q in scopes}
    enclosed = [item for item in definitions if item.enclosing in wanted]
    if enclosed:
        return enclosed
    kept = {
        search_hit_key(hit)
        for hit in resolve_qualified_hits(
            [item.hit for item in definitions], qualifiers, strict=strict
        )
    }
    return [item for item in definitions if search_hit_key(item.hit) in kept]


def order_by_comentions(hits: list[SearchHit], names: Sequence[str]) -> list[SearchHit]:
    """Stable order by how many of the request's other names a chunk mentions.

    Overloads share a name; the parameter types the request spells out
    (``JsonReader``, ``TypeToken``) pick the right one deterministically.
    """
    names = tuple(dict.fromkeys(name for name in names if name))
    if len(hits) < 2 or not names:
        return hits

    def mentions(hit: SearchHit) -> int:
        text = f"{hit.context or ''}\n{hit.content}"
        return sum(_word_in(name, text) for name in names)

    return sorted(hits, key=lambda hit: -mentions(hit))


def _signature_text(lines: Sequence[str], max_lines: int = 4) -> str:
    """The declaration up to the close of its parameter list.

    The body's first statement may name the very type that distinguishes
    another overload (``JsonReader jsonReader = ...`` under ``fromJson(Reader,
    TypeToken)``), so the window ends where the parameters do.
    """
    taken: list[str] = []
    depth = 0
    opened = False
    for line in lines[:max_lines]:
        taken.append(line)
        depth += line.count("(") - line.count(")")
        opened = opened or "(" in line
        if opened and depth <= 0:
            break
    return "\n".join(taken)


def order_by_signature_comentions(
    hits: list[SearchHit], definitions: Sequence[DefinitionHit], names: Sequence[str]
) -> list[SearchHit]:
    """Overloads by the request's other names in their signature, then anywhere.

    A chunk holding several overloads mentions every parameter type of every
    overload in its text; the declaration line of the one asked about is
    what names both ``JsonReader`` and ``TypeToken``.
    """
    names = tuple(dict.fromkeys(name for name in names if name))
    if len(hits) < 2 or not names:
        return hits
    signatures: dict[SearchHitKey, int] = {}
    for definition in definitions:
        chunk = definition.hit
        lines = chunk.content.splitlines()
        offset = definition.start_line - chunk.start_line
        if offset < 0 or offset >= len(lines):
            continue
        signature = _signature_text(lines[offset:])
        count = sum(_word_in(name, signature) for name in names)
        key = search_hit_key(chunk)
        signatures[key] = max(signatures.get(key, 0), count)

    def mentions(hit: SearchHit) -> int:
        text = f"{hit.context or ''}\n{hit.content}"
        return sum(_word_in(name, text) for name in names)

    return sorted(
        hits,
        key=lambda hit: (-signatures.get(search_hit_key(hit), 0), -mentions(hit)),
    )


_MINED_IDENTIFIER = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b")


_IDENTIFIER_NOISE = frozenset(
    """
    self cls this super none true false null nil return import from export def
    class fn func function const let var static public private protected async
    await yield lambda pass break continue while for foreach else elif if try
    catch except finally raise throw throws new delete typeof instanceof void
    int str float bool list dict set tuple object string number boolean array
    print len range enumerate zip map filter isinstance hasattr getattr setattr
    type super value values key keys item items name args kwargs result data
    error exception warning test tests assert with match case default switch
    struct enum trait impl mod use pub crate where type interface extends
    implements package namespace using module require console
    """.split()
)


def _mine_identifiers(hits: Sequence[SearchHit]) -> list[str]:
    """Identifiers referenced by the hits, most widely shared first.

    cAST may place an enclosing class or function signature in ``context``
    rather than repeating it in a split method chunk. Treat that structural
    header as part of the hit for relation expansion, otherwise base classes
    disappear precisely when chunking is most accurate.
    """
    per_hit: dict[str, set[int]] = {}
    total: dict[str, int] = {}
    for index, hit in enumerate(hits):
        for text in (hit.content, hit.context or ""):
            for match in _MINED_IDENTIFIER.finditer(text):
                token = match.group()
                lowered = token.lower()
                if lowered in _IDENTIFIER_NOISE:
                    continue
                # A plain lowercase word needs some length to look like a symbol.
                if token.islower() and "_" not in token and len(token) < 6:
                    continue
                per_hit.setdefault(token, set()).add(index)
                total[token] = total.get(token, 0) + 1
    return sorted(
        per_hit,
        key=lambda token: (-len(per_hit[token]), -total[token], token),
    )
