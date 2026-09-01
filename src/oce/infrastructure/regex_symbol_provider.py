"""Conservative regex-backed definition and endpoint evidence."""

from __future__ import annotations

import re

from oce.domain.services.symbols import SymbolKind, SymbolOccurrence


class RegexSymbolProvider:
    """Extract the definition evidence supported by the current exact index."""

    _ENDPOINT_PATTERNS = (
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
    _DEFINITION_PATTERNS = (
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

    def extract(
        self,
        *,
        content: str,
        language: str | None,
        start_line: int,
        end_line: int,
    ) -> tuple[SymbolOccurrence, ...]:
        """Return de-duplicated evidence while preserving endpoint priority."""
        _ = language
        symbols: dict[tuple[str, str], SymbolOccurrence] = {}

        for pattern in self._ENDPOINT_PATTERNS:
            for match in pattern.finditer(content):
                self._add(
                    symbols,
                    match.group(1),
                    "endpoint",
                    start_line,
                    end_line,
                )

        for pattern in self._DEFINITION_PATTERNS:
            for match in pattern.finditer(content):
                identifier = match.group(1)
                if (identifier, "endpoint") not in symbols:
                    self._add(
                        symbols,
                        identifier,
                        "definition",
                        start_line,
                        end_line,
                    )

        return tuple(symbols.values())

    @staticmethod
    def _add(
        symbols: dict[tuple[str, str], SymbolOccurrence],
        identifier: str,
        kind: SymbolKind,
        start_line: int,
        end_line: int,
    ) -> None:
        if len(identifier) < 2:
            return
        key = (identifier, kind)
        if key in symbols:
            return
        symbols[key] = SymbolOccurrence(
            identifier=identifier,
            kind=kind,
            start_line=start_line,
            end_line=end_line,
        )
