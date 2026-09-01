"""Structural symbol evidence produced while indexing source chunks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, Sequence

if TYPE_CHECKING:
    from oce.domain.blob.blob import Blob
    from oce.domain.chunk import Chunk


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


class SymbolProjection(Protocol):
    """Persist structural evidence when a blob's immutable chunks are created."""

    async def index(self, blob: Blob, chunks: Sequence[Chunk]) -> None: ...
