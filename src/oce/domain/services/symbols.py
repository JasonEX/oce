"""Structural symbol evidence produced while indexing source files."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from oce.domain.blob.blob import Blob
    from oce.domain.chunk import Chunk


# endpoint: route/command handlers; definition: declared names; import: names a
# file pulls in; call: names a file invokes. Imports and calls only serve
# reference/call-chain lookups and never count as structural evidence that a
# symbol question has been answered.
SymbolKind = Literal["endpoint", "definition", "import", "call"]

DEFINITION_KINDS: tuple[str, ...] = ("endpoint", "definition")
CALL_KIND = "call"


@dataclass(frozen=True)
class SymbolOccurrence:
    """One symbol occurrence with absolute 1-based file lines."""

    identifier: str
    kind: SymbolKind
    start_line: int
    end_line: int


class SymbolProvider(Protocol):
    """Produce structural evidence for one whole file, independent of chunking."""

    def extract(
        self,
        *,
        content: str,
        language: str | None,
    ) -> Sequence[SymbolOccurrence]: ...


class SymbolProjection(Protocol):
    """Persist structural evidence when a blob's immutable chunks are created."""

    async def index(
        self,
        blob: Blob,
        chunks: Sequence[Chunk],
        content: str,
    ) -> None: ...
