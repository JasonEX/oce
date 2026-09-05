"""tree-sitter symbol evidence: definitions and imports for one whole file.

The walk is grammar-agnostic (see ``declarations``): a node defines a symbol
when its type looks declarative and it carries a name. Endpoints stay a
textual convention detected by the regex provider, which also covers
languages without a loadable grammar.
"""

from __future__ import annotations

from collections.abc import Sequence

from loguru import logger
from tree_sitter_language_pack import get_parser

from oce.domain.services.symbols import SymbolKind, SymbolOccurrence, SymbolProvider
from oce.infrastructure.astchunk.astchunk_builder import LANGUAGE_MAP
from oce.infrastructure.astchunk.compat import CompatNode, compat_parse
from oce.infrastructure.astchunk.declarations import (
    _leaf_name,
    callee_name,
    declared_name,
    heritage_names,
    is_call_type,
    is_definition_type,
    is_function_like,
    require_alias_names,
)
from oce.infrastructure.regex_symbol_provider import LineIndex, find_endpoints

# Files above this size fall back to regex evidence: a full node walk of a
# generated megabyte would dominate indexing time for one low-value file.
MAX_TREE_SITTER_BYTES = 512 * 1024

_IMPORT_TYPES = frozenset(
    {"use_declaration", "using_directive", "using_declaration", "require_call"}
)
_STRING_TYPES = frozenset(
    {"string", "string_literal", "interpreted_string_literal", "raw_string_literal"}
)
_IMPORT_NOISE = frozenset({"as", "from", "import", "use", "using", "self", "super"})
# Python ``assignment`` and JavaScript/TypeScript ``assignment_expression``
# both declare module-level names; see ``declared_name``.
_ASSIGNMENT_TYPES = frozenset({"assignment", "assignment_expression"})
# Callee names too common to locate anything: language builtins and the verbs
# every codebase repeats. Kept short on purpose; rarity is scored at query time.
_CALL_NOISE = frozenset(
    """
    print len str int float bool list dict set tuple range enumerate zip map filter
    isinstance hasattr getattr setattr format join split strip append extend pop
    get keys values items open super type id repr sorted reversed min max sum abs
    any all next iter push slice concat log error warn info debug require
    string number parse assert expect vec some ok err box new from into unwrap
    clone default println format write writeln sizeof malloc free memcpy strlen
    strcmp printf sprintf fprintf tostring valueof equals hashcode length size
    """.split()
)
_MAX_CALLS_PER_FILE = 3_000
# A grammar download can fail transiently; do not pin the failure for the
# life of the process, but stop retrying once it is clearly unavailable.
_PARSER_ATTEMPTS = 3
_OPAQUE_TYPES = frozenset(
    {
        "comment",
        "string",
        "string_literal",
        "template_string",
        "interpreted_string_literal",
    }
)


# Documentation renders code, it does not declare it: a fenced ``def`` in a
# README is prose about the project. Regex fallback on these files produced
# "definitions" that outnumbered the real one and damped its exact score.
PROSE_LANGUAGES = frozenset(
    {"markdown", "rst", "restructuredtext", "text", "plaintext", "asciidoc", "org"}
)
PROSE_SUFFIXES = (
    ".md",
    ".mdx",
    ".markdown",
    ".rst",
    ".txt",
    ".adoc",
    ".asciidoc",
    ".org",
)


def is_prose_language(language: str | None) -> bool:
    return language is not None and language.lower() in PROSE_LANGUAGES


class TreeSitterSymbolProvider:
    def __init__(self, fallback: SymbolProvider) -> None:
        self._fallback = fallback
        self._parsers: dict[str, object | None] = {}
        self._failures: dict[str, int] = {}

    def extract(
        self,
        *,
        content: str,
        language: str | None,
        path: str | None = None,
    ) -> Sequence[SymbolOccurrence]:
        if is_prose_language(language):
            return ()
        parser = self._parser(language) if language else None
        if parser is None or len(content) > MAX_TREE_SITTER_BYTES:
            return self._fallback.extract(content=content, language=language)
        if len(content.encode("utf-8")) > MAX_TREE_SITTER_BYTES:
            return self._fallback.extract(content=content, language=language)
        try:
            root = compat_parse(parser, content).root_node
        except Exception as exc:
            logger.warning(
                "tree-sitter parse failed for {}; using regex symbols: {}",
                language,
                type(exc).__name__,
            )
            return self._fallback.extract(content=content, language=language)

        endpoints = find_endpoints(content, LineIndex(content))
        occurrences: dict[tuple[str, str, int, str], SymbolOccurrence] = {}
        calls = 0
        barrel = is_barrel_path(path)
        package = _package_name(path) if barrel else None

        def add(
            identifier: str, kind: SymbolKind, start: int, end: int, enclosing: str
        ) -> None:
            if len(identifier) < 2:
                return
            key = (identifier, kind, start, enclosing)
            if key not in occurrences:
                occurrences[key] = SymbolOccurrence(
                    identifier, kind, start, end, enclosing
                )

        def add_call(node: CompatNode, enclosing: str) -> None:
            nonlocal calls
            if calls >= _MAX_CALLS_PER_FILE:
                return
            name = callee_name(node)
            if name is None or len(name) < 3 or name.lower() in _CALL_NOISE:
                return
            line = node.start_point.row + 1
            key = (name, "call", line, enclosing)
            if key not in occurrences:
                occurrences[key] = SymbolOccurrence(name, "call", line, line, enclosing)
                calls += 1

        # (node, inside_function, enclosing): locals declared inside a function
        # body are not project symbols, but nested functions and classes still
        # are. ``enclosing`` is the innermost named definition above the node.
        stack: list[tuple[CompatNode, bool, str]] = [(root, False, "")]
        while stack:
            node, inside_function, enclosing = stack.pop()
            for child in reversed(node.named_children):
                child_type = child.type
                start = child.start_point.row + 1
                end = child.end_point.row + 1
                if child_type.startswith("import") or child_type in _IMPORT_TYPES:
                    names = _import_names(child)
                    for name in names:
                        add(name, "import", start, end, enclosing)
                    for name in _reexport_names(child, names, package=package):
                        add(name, "reexport", start, end, enclosing)
                    continue
                if child_type == "export_statement" and (
                    child.child_by_field_name("source") is not None
                ):
                    # ``export { X } from './x'`` forwards a name declared in
                    # another module; the specifier itself declares nothing.
                    for name in _export_specifier_names(child):
                        add(name, "import", start, end, enclosing)
                        add(name, "reexport", start, end, enclosing)
                    continue
                aliases = require_alias_names(child)
                if aliases is not None:
                    for name in aliases:
                        add(name, "import", start, end, enclosing)
                    continue
                # Call sites provide direct-use evidence to reference and
                # call-chain lookups; structural answer heads never use them.
                if is_call_type(child_type):
                    add_call(child, enclosing)
                # Anything function-shaped (declaration, arrow, lambda, closure)
                # turns the declarators below it into locals.
                descend_inside_function = inside_function or is_function_like(
                    child_type
                )
                child_enclosing = enclosing
                if is_definition_type(child_type) or child_type in _ASSIGNMENT_TYPES:
                    name = declared_name(child)
                    is_local = inside_function and (
                        child_type.endswith("_declarator")
                        or child_type in _ASSIGNMENT_TYPES
                        or child_type in ("property_declaration", "let_declaration")
                    )
                    if name is not None and not is_local:
                        kind = "endpoint" if name in endpoints else "definition"
                        add(name, kind, start, end, enclosing)
                        child_enclosing = name
                        for base in heritage_names(child):
                            add(base, "inherit", start, end, name)
                if child_type == "impl_item":
                    # ``impl Trait for Type`` declares nothing new, but its
                    # methods belong to ``Type`` and it implements ``Trait``.
                    subject = child.child_by_field_name("type")
                    trait = child.child_by_field_name("trait")
                    subject_name = _leaf_name(subject) if subject is not None else None
                    if subject_name:
                        child_enclosing = subject_name
                        trait_name = _leaf_name(trait) if trait is not None else None
                        if trait_name:
                            add(trait_name, "inherit", start, end, subject_name)
                # Strings and comments never hold declarations; everything
                # else may (one-line classes, impl blocks, nested closures).
                if child_type in _OPAQUE_TYPES or not child.named_children:
                    continue
                stack.append((child, descend_inside_function, child_enclosing))

        # Endpoints the tree walk did not attribute (decorator on a shape the
        # generic rules miss) keep their regex evidence.
        for identifier, line in endpoints.items():
            if not any(
                key[0] == identifier and key[1] == "endpoint" for key in occurrences
            ):
                add(identifier, "endpoint", line, line, "")
        return tuple(occurrences.values())

    def _parser(self, language: str):
        key = language.lower()
        if key in self._parsers:
            return self._parsers[key]
        grammar = LANGUAGE_MAP.get(key)
        if grammar is None:
            self._parsers[key] = None
            return None
        if self._failures.get(key, 0) >= _PARSER_ATTEMPTS:
            return None
        try:
            parser = get_parser(grammar)
        except Exception as exc:
            self._failures[key] = self._failures.get(key, 0) + 1
            logger.warning(
                "tree-sitter grammar unavailable for {}; using regex symbols: {}",
                language,
                exc,
            )
            return None
        self._parsers[key] = parser
        return parser


def _import_names(node: CompatNode) -> list[str]:
    """Identifiers an import statement binds or refers to, in source order."""
    names: list[str] = []
    pending = [node]
    while pending:
        current = pending.pop(0)
        children = current.named_children
        if not children:
            text = current.text.decode("utf-8", errors="replace").strip()
            if current.type.endswith("identifier"):
                if text not in _IMPORT_NOISE and text not in names:
                    names.append(text)
            elif current.type in _STRING_TYPES:
                segment = text.strip("\"'`").rstrip("/").rsplit("/", 1)[-1]
                segment = segment.split(".", 1)[0]
                if (
                    segment
                    and segment not in names
                    and segment.replace("_", "").isalnum()
                ):
                    names.append(segment)
            continue
        if current.type in _STRING_TYPES:
            text = current.text.decode("utf-8", errors="replace").strip("\"'`")
            segment = text.rstrip("/").rsplit("/", 1)[-1].split(".", 1)[0]
            if segment and segment not in names and segment.replace("_", "").isalnum():
                names.append(segment)
            continue
        pending.extend(children)
    return names


def is_barrel_path(path: str | None) -> bool:
    """Package entry files whose relative imports republish other modules."""
    if not path:
        return False
    name = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name in _BARREL_FILENAMES


_BARREL_FILENAMES = frozenset(
    {
        "__init__.py",
        "index.ts",
        "index.tsx",
        "index.js",
        "index.jsx",
        "mod.rs",
        "lib.rs",
    }
)


def _package_name(path: str | None) -> str | None:
    """Directory holding a barrel file: the package its absolute imports name."""
    if not path:
        return None
    parts = path.replace("\\", "/").split("/")
    return parts[-2] if len(parts) >= 2 and parts[-2] else None


def _reexport_names(
    node: CompatNode, imported: Sequence[str], *, package: str | None
) -> list[str]:
    """Names an import statement republishes to importers of this file.

    Rust ``pub use`` re-exports wherever it appears. Inside a package entry
    file (``package`` is its directory name) a relative import or an absolute
    import of the package's own modules (``from .app import Flask``,
    ``from billing.invoice import build_invoice``) is the Python idiom for
    the same thing. Path segments are not republished names, only the bound
    leaves are.
    """
    if node.type == "use_declaration":
        if not any(
            child.type == "visibility_modifier" for child in node.named_children
        ):
            return []
        argument = node.child_by_field_name("argument")
        return _use_leaf_names(argument) if argument is not None else []
    if node.type == "import_from_statement":
        module = node.child_by_field_name("module_name")
        if package is None or module is None:
            return []
        module_text = module.text.decode("utf-8", errors="replace")
        if module.type != "relative_import" and (
            module_text.split(".", 1)[0] != package
        ):
            return []
        leaves: list[str] = []
        # Every named child after the module is an imported name or alias.
        for child in node.named_children:
            if child is module or child.type == "relative_import":
                continue
            target = child
            if child.type == "aliased_import":
                target = child.child_by_field_name("alias") or child
            identifiers = [
                item
                for item in (target, *target.named_children)
                if item.type.endswith("identifier") and not item.named_children
            ]
            if identifiers:
                text = identifiers[-1].text.decode("utf-8", errors="replace")
                if text in imported and text not in leaves:
                    leaves.append(text)
        return leaves
    return []


def _use_leaf_names(node: CompatNode) -> list[str]:
    """Bound names of a Rust ``use`` tree: ``a::b::{C, D as E}`` -> C, E."""
    if node.type.endswith("identifier") and not node.named_children:
        return [node.text.decode("utf-8", errors="replace")]
    if node.type == "use_as_clause":
        alias = node.child_by_field_name("alias")
        return _use_leaf_names(alias) if alias is not None else []
    if node.type in ("scoped_identifier", "scoped_use_list"):
        for field in ("list", "name"):
            child = node.child_by_field_name(field)
            if child is not None:
                return _use_leaf_names(child)
        return []
    if node.type == "use_list":
        names: list[str] = []
        for child in node.named_children:
            for name in _use_leaf_names(child):
                if name not in names and name not in _IMPORT_NOISE:
                    names.append(name)
        return names
    return []


def _export_specifier_names(node: CompatNode) -> list[str]:
    names: list[str] = []
    pending = list(node.named_children)
    while pending:
        current = pending.pop(0)
        if current.type == "export_specifier":
            target = current.child_by_field_name(
                "alias"
            ) or current.child_by_field_name("name")
            if target is not None:
                text = target.text.decode("utf-8", errors="replace")
                if text not in names:
                    names.append(text)
            continue
        pending.extend(current.named_children)
    return names
