"""One syntax node together with its measured non-whitespace size."""

from __future__ import annotations

from oce.infrastructure.astchunk.compat import CompatNode


class ASTNode:
    __slots__ = ("node", "size")

    def __init__(self, node: CompatNode, size: int) -> None:
        self.node = node
        self.size = size

    @property
    def start_row(self) -> int:
        return self.node.start_point.row

    @property
    def end_row(self) -> int:
        return self.node.end_point.row

    @property
    def end_column(self) -> int:
        return self.node.end_point.column
