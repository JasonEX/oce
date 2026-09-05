"""Structural symbol evidence produced while indexing source files."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from oce.domain.blob.blob import Blob
    from oce.domain.chunk import Chunk


# endpoint: route/command handlers; definition: declared names; import: names a
# file pulls in; call: names a file invokes; reexport: names a barrel file
# forwards from another module (``export { X } from``, ``pub use``, relative
# imports in ``__init__.py``); inherit: base classes, interfaces and traits a
# declaration extends or implements (``enclosing`` is the subtype). Imports,
# calls, re-exports and inheritance only serve reference/call-chain/relation
# lookups and never count as structural evidence that a symbol question has
# been answered.
SymbolKind = Literal["endpoint", "definition", "import", "call", "reexport", "inherit"]

DEFINITION_KINDS: tuple[str, ...] = ("endpoint", "definition")
CALL_KIND = "call"
IMPORT_KIND = "import"
REEXPORT_KIND = "reexport"
INHERIT_KIND = "inherit"
# Kinds a file header may consist of without implementing anything.
HEADER_KINDS: tuple[str, ...] = (IMPORT_KIND, REEXPORT_KIND)


@dataclass(frozen=True)
class SymbolOccurrence:
    """One symbol occurrence with absolute 1-based file lines.

    ``enclosing`` names the innermost definition the occurrence sits in
    (``Service`` for a method, ``run`` for a call inside ``run``); it is empty
    at module level or when the provider cannot tell. It is what turns a call
    row into a caller-to-callee edge.
    """

    identifier: str
    kind: SymbolKind
    start_line: int
    end_line: int
    enclosing: str = ""


class SymbolProvider(Protocol):
    """Produce structural evidence for one whole file, independent of chunking."""

    def extract(
        self,
        *,
        content: str,
        language: str | None,
        path: str | None = None,
    ) -> Sequence[SymbolOccurrence]:
        """``path`` lets barrel-file rules (``__init__.py``) apply; it may be omitted."""
        ...


class SymbolProjection(Protocol):
    """Persist structural evidence when a blob's immutable chunks are created."""

    async def index(
        self,
        blob: Blob,
        chunks: Sequence[Chunk],
        content: str,
    ) -> None: ...
