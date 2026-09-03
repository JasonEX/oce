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
    declared_name,
    is_definition_type,
    is_function_like,
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
_OPAQUE_TYPES = frozenset(
    {
        "comment",
        "string",
        "string_literal",
        "template_string",
        "interpreted_string_literal",
    }
)


class TreeSitterSymbolProvider:
    def __init__(self, fallback: SymbolProvider) -> None:
        self._fallback = fallback
        self._parsers: dict[str, object | None] = {}

    def extract(
        self,
        *,
        content: str,
        language: str | None,
    ) -> Sequence[SymbolOccurrence]:
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
        occurrences: dict[tuple[str, str, int], SymbolOccurrence] = {}

        def add(identifier: str, kind: SymbolKind, start: int, end: int) -> None:
            if len(identifier) < 2:
                return
            key = (identifier, kind, start)
            if key not in occurrences:
                occurrences[key] = SymbolOccurrence(identifier, kind, start, end)

        # (node, inside_function): locals declared inside a function body are
        # not project symbols, but nested functions and classes still are.
        stack: list[tuple[CompatNode, bool]] = [(root, False)]
        while stack:
            node, inside_function = stack.pop()
            for child in reversed(node.named_children):
                child_type = child.type
                start = child.start_point.row + 1
                end = child.end_point.row + 1
                if child_type.startswith("import") or child_type in _IMPORT_TYPES:
                    for name in _import_names(child):
                        add(name, "import", start, end)
                    continue
                # Anything function-shaped (declaration, arrow, lambda, closure)
                # turns the declarators below it into locals.
                descend_inside_function = inside_function or is_function_like(
                    child_type
                )
                if is_definition_type(child_type) or child_type == "assignment":
                    name = declared_name(child)
                    is_local = inside_function and (
                        child_type.endswith("_declarator")
                        or child_type in ("assignment", "property_declaration")
                    )
                    if name is not None and not is_local:
                        kind = "endpoint" if name in endpoints else "definition"
                        add(name, kind, start, end)
                # Strings and comments never hold declarations; everything
                # else may (one-line classes, impl blocks, nested closures).
                if child_type in _OPAQUE_TYPES or not child.named_children:
                    continue
                stack.append((child, descend_inside_function))

        # Endpoints the tree walk did not attribute (decorator on a shape the
        # generic rules miss) keep their regex evidence.
        for identifier, line in endpoints.items():
            if not any(
                key[0] == identifier and key[1] == "endpoint" for key in occurrences
            ):
                add(identifier, "endpoint", line, line)
        return tuple(occurrences.values())

    def _parser(self, language: str):
        key = language.lower()
        if key not in self._parsers:
            grammar = LANGUAGE_MAP.get(key)
            parser = None
            if grammar is not None:
                try:
                    parser = get_parser(grammar)
                except Exception as exc:
                    logger.warning(
                        "tree-sitter grammar unavailable for {}; using regex symbols: {}",
                        language,
                        exc,
                    )
            self._parsers[key] = parser
        return self._parsers[key]


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
