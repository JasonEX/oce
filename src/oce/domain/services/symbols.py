"""Structural symbol evidence produced while indexing source chunks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, Sequence


SymbolKind = Literal["endpoint", "definition", "reference"]


@dataclass(frozen=True)
class SymbolOccurrence:
    """One symbol occurrence associated with an indexed source span."""

    identifier: str
    kind: SymbolKind
    start_line: int
    end_line: int


class SymbolProvider(Protocol):
    """Produce structural evidence without coupling persistence to a parser."""

    def extract(
        self,
        *,
        content: str,
        language: str | None,
        start_line: int,
        end_line: int,
    ) -> Sequence[SymbolOccurrence]: ...
