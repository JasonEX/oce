"""Snapshot wrappers over tree-sitter 0.25+ nodes.

Native nodes can become invalid while a tree is walked; reading a stale one
is a native access violation on Windows that no Python ``except`` catches.
Every scalar (type, byte range, points, named flag, child references, field
names) is copied into Python objects at construction and the native node is
never touched again. ``text`` slices the source bytes by the snapshotted
range instead of calling the native property, and ``_tree`` is held so the
tree is not collected under the wrappers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tree_sitter import Node, Parser, Tree


class _Point:
    """A snapshotted (row, column) point."""

    __slots__ = ("row", "column")

    def __init__(self, row: int, column: int):
        self.row = row
        self.column = column

    def __repr__(self) -> str:
        return f"Point(row={self.row}, column={self.column})"


class CompatNode:
    """A node whose scalars were copied at construction; see the module docstring."""

    __slots__ = (
        "_child_refs",  # native children, taken while the parent was valid
        "_source",  # the whole source as bytes
        "_tree",  # keeps the native tree alive
        "type",
        "start_byte",
        "end_byte",
        "start_point",
        "end_point",
        "is_named",
        "_field_names",  # one per child, same order as _child_refs
        "_children_cache",  # wrapped children, built on first access
    )

    def __init__(self, node: Node, source: bytes, tree: Tree):
        self._source = source
        self._tree = tree
        self._children_cache: list[CompatNode] | None = None

        self.type = node.type
        self.start_byte = node.start_byte
        self.end_byte = node.end_byte
        self.is_named = node.is_named

        # Points are native objects too and are copied the same way.
        sp = node.start_point
        self.start_point = _Point(sp.row, sp.column)
        ep = node.end_point
        self.end_point = _Point(ep.row, ep.column)

        # tree-sitter 0.26 on Windows may return an invalid temporary node from
        # repeated node.child(i) calls; taking every child once while the
        # parent is valid pins their lifetime.
        self._child_refs = tuple(node.children)
        # Field names must be read now as well: they come from the parent's
        # cursor, which is inaccessible after the snapshot. Before 0.25 the
        # method did not exist; without it callers fall back to node types.
        field_lookup = getattr(node, "field_name_for_child", None)
        if field_lookup is None:
            self._field_names = (None,) * len(self._child_refs)
        else:
            self._field_names = tuple(
                field_lookup(index) for index in range(len(self._child_refs))
            )

    @property
    def text(self) -> bytes:
        """The node's source bytes, sliced by the snapshotted range."""
        return self._source[self.start_byte : self.end_byte]

    @property
    def children(self) -> list[CompatNode]:
        """Wrapped children, built from the native references on first access."""
        cached = self._children_cache
        if cached is None:
            cached = [
                CompatNode(child, self._source, self._tree)
                for child in self._child_refs
            ]
            self._children_cache = cached
            self._child_refs = ()
        return cached

    @property
    def named_children(self) -> list[CompatNode]:
        """Children that are named grammar nodes, not punctuation."""
        return [child for child in self.children if child.is_named]

    def child_by_field_name(self, field: str) -> CompatNode | None:
        """The child under ``field``, matching the native method's semantics."""
        for child, name in zip(self.children, self._field_names, strict=True):
            if name == field:
                return child
        return None


class CompatTree:
    """A parsed tree whose root is served as a ``CompatNode``."""

    __slots__ = ("_tree", "_source")

    def __init__(self, tree: Tree, source: bytes):
        self._tree = tree
        self._source = source

    @property
    def root_node(self) -> CompatNode:
        return CompatNode(self._tree.root_node, self._source, self._tree)


def compat_parse(parser: Parser, code: str) -> CompatTree:
    """Parse ``code`` and wrap the tree; ``parse`` takes bytes in 0.25+, so it is encoded here."""
    source_bytes = code.encode("utf-8")
    tree = parser.parse(source_bytes)
    return CompatTree(tree, source_bytes)
