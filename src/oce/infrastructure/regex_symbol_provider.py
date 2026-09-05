"""Conservative regex-backed definition and endpoint evidence.

Serves two roles: the whole provider for languages without a tree-sitter
grammar, and the endpoint detector that the tree-sitter provider reuses,
because route/command decorators are a textual convention rather than a
syntactic one.
"""

from __future__ import annotations

import bisect
import re
from collections.abc import Sequence

from oce.domain.services.symbols import SymbolKind, SymbolOccurrence

ENDPOINT_PATTERNS = (
    re.compile(
        r"(?ms)#\[(?:tauri::command|pytauri::command)[^\]]*\]\s*"
        r"(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)\b"
    ),
    re.compile(
        r"(?m)@(?:app|router)\.(?:get|post|put|patch|delete|websocket)\([^\n]*\)\s*"
        r"(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\b"
    ),
    re.compile(
        r"(?m)@(?:Get|Post|Put|Patch|Delete|Controller)\([^\n]*\)\s*"
        r"(?:async\s+)?(?:function\s+)?([A-Za-z_$][A-Za-z0-9_$]*)\b"
    ),
)
DEFINITION_PATTERNS = (
    re.compile(
        r"(?m)^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?"
        r"(?:fn|struct|enum|trait|type|const|static)\s+([A-Za-z_][A-Za-z0-9_]*)\b"
    ),
    re.compile(r"(?m)^\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)\b"),
    re.compile(
        r"(?m)^\s*(?:export\s+)?(?:async\s+)?(?:default\s+)?"
        r"(?:function|class|interface|type|enum)\s+([A-Za-z_$][A-Za-z0-9_$]*)\b"
    ),
    re.compile(
        r"(?m)^\s*(?:export\s+)?(?:const|let|var)\s+"
        r"([A-Za-z_$][A-Za-z0-9_$]*)\s*="
    ),
)


class LineIndex:
    """Map character offsets of a text to 1-based line numbers."""

    def __init__(self, content: str) -> None:
        self._starts = [0]
        for index, char in enumerate(content):
            if char == "\n":
                self._starts.append(index + 1)

    def line_of(self, offset: int) -> int:
        return bisect.bisect_right(self._starts, offset)


def find_endpoints(content: str, lines: LineIndex | None = None) -> dict[str, int]:
    """Identifier → line of every endpoint-style handler in the file."""
    lines = lines or LineIndex(content)
    found: dict[str, int] = {}
    for pattern in ENDPOINT_PATTERNS:
        for match in pattern.finditer(content):
            found.setdefault(match.group(1), lines.line_of(match.start(1)))
    return found


class RegexSymbolProvider:
    """Extract the definition evidence supported by the current exact index."""

    def extract(
        self,
        *,
        content: str,
        language: str | None,
        path: str | None = None,
    ) -> Sequence[SymbolOccurrence]:
        """Return de-duplicated evidence while preserving endpoint priority."""
        _ = language, path
        lines = LineIndex(content)
        symbols: dict[tuple[str, str], SymbolOccurrence] = {}

        for identifier, line in find_endpoints(content, lines).items():
            self._add(symbols, identifier, "endpoint", line)

        for pattern in DEFINITION_PATTERNS:
            for match in pattern.finditer(content):
                identifier = match.group(1)
                if (identifier, "endpoint") not in symbols:
                    self._add(
                        symbols, identifier, "definition", lines.line_of(match.start(1))
                    )

        return tuple(symbols.values())

    @staticmethod
    def _add(
        symbols: dict[tuple[str, str], SymbolOccurrence],
        identifier: str,
        kind: SymbolKind,
        line: int,
    ) -> None:
        if len(identifier) < 2:
            return
        key = (identifier, kind)
        if key in symbols:
            return
        # A regex sees no block structure, so a definition spans its own line.
        symbols[key] = SymbolOccurrence(
            identifier=identifier,
            kind=kind,
            start_line=line,
            end_line=line,
        )
