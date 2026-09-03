"""Greedy cAST window assignment.

Windows are tentative chunks measured in non-whitespace characters. The
adapter (``cast_chunker``) turns them into line ranges and cuts the text from
the source, so a window only needs to report where it starts and ends.
"""

from __future__ import annotations

import re
from collections.abc import Generator, Sequence
from dataclasses import dataclass

import numpy as np
from tree_sitter_language_pack import get_parser

from oce.infrastructure.astchunk.astnode import ASTNode
from oce.infrastructure.astchunk.compat import CompatNode, compat_parse
from oce.infrastructure.astchunk.declarations import declared_name, signature_line
from oce.infrastructure.astchunk.preprocessing import (
    ByteRange,
    get_nws_count,
    preprocess_nws_count,
)

# Language name mapping for tree-sitter-language-pack
# Maps user-friendly names to tree-sitter-language-pack language identifiers
LANGUAGE_MAP = {
    # Original supported languages
    "python": "python",
    "java": "java",
    "csharp": "c_sharp",
    "c_sharp": "c_sharp",
    "typescript": "tsx",
    "tsx": "tsx",
    # Additional languages supported by tree-sitter-language-pack
    "javascript": "javascript",
    "jsx": "javascript",
    "c": "c",
    "cpp": "cpp",
    "c++": "cpp",
    "go": "go",
    "golang": "go",
    "rust": "rust",
    "ruby": "ruby",
    "php": "php",
    "swift": "swift",
    "kotlin": "kotlin",
    "scala": "scala",
    "bash": "bash",
    "shell": "bash",
    "sql": "sql",
    "lua": "lua",
    "r": "r",
    "julia": "julia",
    "haskell": "haskell",
    "elixir": "elixir",
    "erlang": "erlang",
    "clojure": "clojure",
    "ocaml": "ocaml",
    "zig": "zig",
    "nim": "nim",
    "dart": "dart",
    "perl": "perl",
    "dockerfile": "dockerfile",
    "make": "make",
    "cmake": "cmake",
}

# Field names tree-sitter grammars use for the payload of a declaration. A node
# carrying one reads as a unit: whoever lands on it sees a subject and the code
# that belongs to it. Splitting one yields halves that open on `return {` or
# trail off mid-statement, which is what the intact-node budget below prevents.
#
# Matching on fields rather than node type names keeps this working per language.
# A type list has to be extended for every grammar and fails silently otherwise:
# measured on OpenClaw, Kotlin kept only 47.8% of its oversized declarations
# intact and Swift 53.8%, because their builder-DSL nodes (`annotated_lambda`,
# `call_suffix`) never appear in a TypeScript-derived list.
_DECLARATION_BODY_FIELDS = (
    "body",
    "block",
    "declaration_list",
    "field_declaration_list",
)

# Some grammars express structure through child node types instead of fields —
# Kotlin names none of its children, so a `class_declaration` there is only
# recognisable by the `class_body` hanging under it. Suffix matching covers the
# `*_body` / `*_block` convention these grammars follow.
_BODY_NODE_SUFFIXES = ("_body", "_block", "_statements", "_declaration_list")
_BODY_NODE_TYPES = frozenset({"block", "statements", "statement_block"})

# Node types that wrap a declaration without being one. Their own body belongs to
# the inner declaration, so they qualify only if that declaration does.
_DECLARATION_WRAPPER_TYPES = frozenset(
    {
        "decorated_definition",
        "export_statement",
        "expression_statement",
        "lexical_declaration",
        "variable_declaration",
        "variable_declarator",
        "public_field_definition",
        "property_declaration",
        "call_expression",
        "arguments",
        # Kotlin/Swift builder DSL: `android { ... }` parses as a call whose
        # trailing lambda holds the block.
        "call_suffix",
        "annotated_lambda",
        "lambda_literal",
        "function_body",
        "assignment",
    }
)

# How far past ``max_chunk_size`` a declaration may run before it is split
# anyway. Measured on the OpenClaw suite: test cases and handlers cluster
# between one and three times the window, so recursing into them was what broke
# two thirds of the chunks, while a hard ceiling still keeps a runaway
# ``describe`` block (150k characters and up) from becoming one chunk.
_INTACT_NODE_SIZE_FACTOR = 3

# Deepest wrapper chain followed when looking for the declaration inside a
# statement. `export default foo(() => {...})` needs a few hops; beyond that the
# node is an expression tree rather than a declaration.
_WRAPPER_SEARCH_DEPTH = 4

_REACT_COMPONENT_LANGUAGES = frozenset({"jsx", "tsx"})
_REACT_DECLARATION_TYPES = frozenset(
    {
        "class_declaration",
        "function_declaration",
        "lexical_declaration",
        "variable_declaration",
    }
)
_JSX_NODE_TYPES = frozenset({"jsx_element", "jsx_fragment", "jsx_self_closing_element"})
_PASCAL_CASE_DECLARATION = re.compile(
    r"^(?:(?:async\s+)?function|class|const|let|var)\s+"
    r"([A-Z][A-Za-z0-9_$]*)\b"
)


@dataclass(frozen=True)
class AstWindow:
    """Zero-based row span of one window; ``end_column`` is the column after its last byte."""

    start_row: int
    end_row: int
    end_column: int


class ASTChunkBuilder:
    """Assign one language's syntax tree to windows of bounded size.

    ``max_chunk_size`` is the non-whitespace budget of a window. ``intact_node_size``
    is the largest declaration kept whole even though it overshoots that budget.
    Constructing a builder for a language without a loadable tree-sitter grammar
    raises, so callers can fall back to text chunking.
    """

    def __init__(
        self,
        *,
        max_chunk_size: int,
        language: str,
        intact_node_size: int | None = None,
    ) -> None:
        if max_chunk_size < 1:
            raise ValueError("max_chunk_size must be positive")
        self.max_chunk_size = max_chunk_size
        self.language = language.lower()
        self.intact_node_size = (
            intact_node_size
            if intact_node_size is not None
            else max_chunk_size * _INTACT_NODE_SIZE_FACTOR
        )
        if self.language not in LANGUAGE_MAP:
            raise ValueError(f"No tree-sitter grammar for language: {language}")
        self.parser = get_parser(LANGUAGE_MAP[self.language])

    def assign_tree_to_windows(
        self, code: str, root_node: CompatNode
    ) -> Generator[list[ASTNode], None, None]:
        """Yield windows for a whole tree.

        Precomputes the non-whitespace prefix sums once, keeps a tree that fits
        the budget as a single window, and otherwise hands the top-level nodes
        to ``assign_nodes_to_windows``.
        """
        # Preprocessing non-whitespace character count
        nws_cumsum = preprocess_nws_count(bytes(code, "utf8"))
        if self.language in _REACT_COMPONENT_LANGUAGES:
            component_windows = list(
                self._assign_react_top_level_windows(
                    root_node,
                    nws_cumsum,
                )
            )
            if component_windows:
                yield from component_windows
                return

        tree_range = ByteRange(root_node.start_byte, root_node.end_byte)
        tree_size = get_nws_count(nws_cumsum, tree_range)

        if tree_size == 0:
            return
        # If the entire tree can fit in one window, assign tree to window
        if tree_size <= self.max_chunk_size:
            yield [ASTNode(root_node, tree_size)]
        # Otherwise, recursively assign children to windows
        else:
            yield from self.assign_nodes_to_windows(root_node.children, nws_cumsum)

    def _assign_react_top_level_windows(
        self,
        root_node: CompatNode,
        nws_cumsum: np.ndarray,
    ) -> Generator[list[ASTNode], None, None]:
        """Keep each top-level JSX/TSX component on its own AST boundary."""
        nodes = root_node.children
        if not any(self._is_react_component(node) for node in nodes):
            return

        ordinary_nodes: list[CompatNode] = []
        for node in nodes:
            if not self._is_react_component(node):
                ordinary_nodes.append(node)
                continue
            if ordinary_nodes:
                yield from self.assign_nodes_to_windows(ordinary_nodes, nws_cumsum)
                ordinary_nodes = []
            yield from self.assign_nodes_to_windows([node], nws_cumsum)
        if ordinary_nodes:
            yield from self.assign_nodes_to_windows(ordinary_nodes, nws_cumsum)

    def _is_react_component(self, node: CompatNode) -> bool:
        declaration = node
        if node.type == "export_statement":
            declaration = next(
                (
                    child
                    for child in node.children
                    if child.type in _REACT_DECLARATION_TYPES
                ),
                node,
            )
        if declaration.type not in _REACT_DECLARATION_TYPES:
            return False
        if not self._has_pascal_case_name(declaration):
            return False
        return self._contains_jsx(declaration)

    @staticmethod
    def _has_pascal_case_name(node: CompatNode) -> bool:
        first_line = node.text.decode("utf8", errors="replace").splitlines()[0]
        return _PASCAL_CASE_DECLARATION.match(first_line.strip()) is not None

    @staticmethod
    def _contains_jsx(node: CompatNode) -> bool:
        pending = list(node.children)
        while pending:
            current = pending.pop()
            if current.type in _JSX_NODE_TYPES:
                return True
            pending.extend(current.children)
        return False

    def assign_nodes_to_windows(
        self,
        nodes: Sequence[CompatNode],
        nws_cumsum: np.ndarray,
    ) -> Generator[list[ASTNode], None, None]:
        """Greedily pack sibling nodes into windows, recursing into oversized ones."""
        if not nodes:
            return

        # Initialize the current window
        current_window = []
        current_window_size = 0

        for node in nodes:
            node_range = ByteRange(node.start_byte, node.end_byte)
            node_size = get_nws_count(nws_cumsum, node_range)

            # Check if node needs recursive processing (i.e., too large to fit in a window)
            node_exceeds_limit = node_size > self.max_chunk_size

            # Handle the cases where we cannot add the current node to the current window
            # Case 1: current window is empty and node exceeds limit
            # Case 2: current window is not empty and adding the node exceeds limit
            if (len(current_window) == 0 and node_exceeds_limit) or (
                current_window_size + node_size > self.max_chunk_size
            ):
                # Clear current window if not empty
                if len(current_window) > 0:
                    yield current_window
                    current_window = []
                    current_window_size = 0

                # If node still exceeds limit, recursively process the node's children
                if node_exceeds_limit:
                    # A declaration that only modestly overshoots the window is
                    # kept whole. Recursing into it hands back its statements,
                    # and greedy packing then cuts between them, so the chunk
                    # that carries the name loses the body and the next one
                    # opens on a fragment like `return {`.
                    if self._is_intact_declaration(node, node_size):
                        yield [ASTNode(node, node_size)]
                        continue
                    child_windows = list(
                        self.assign_nodes_to_windows(node.children, nws_cumsum)
                    )
                    if child_windows:
                        # (optional) Greedily merge adjacent windows from the beginning if merged window does not exceed self.max_chunk_size
                        yield from self.merge_adjacent_windows(child_windows)
                    else:
                        # An oversized leaf has nothing to recurse into. It is kept
                        # as its own window; cap_span splits it by lines later.
                        yield [ASTNode(node, node_size)]
                else:
                    # Node fits in an empty window
                    current_window.append(ASTNode(node, node_size))
                    current_window_size += node_size

            # Case 3: node fits in current window
            else:
                current_window.append(ASTNode(node, node_size))
                current_window_size += node_size

        # Add the last window if it's not empty
        if len(current_window) > 0:
            yield current_window

    def _is_intact_declaration(self, node: CompatNode, node_size: int) -> bool:
        """Whether a node should stay whole even though it overshoots the window.

        Size is what separates the two shapes a wrapper can take: one
        ``expression_statement`` is a single ``it(...)`` case, another is a
        ``describe`` block spanning a whole file. Past ``intact_node_size`` the
        node is split as before.

        Wrappers like ``export_statement`` are not declarations themselves, so
        the body check stops at depth 0 — the wrapper qualifies only if it
        directly owns a body, not if one hangs off something it wraps.
        """
        return node_size <= self.intact_node_size and self._carries_body(
            node, max_depth=0
        )

    def _carries_body(
        self, node: CompatNode, depth: int = 0, max_depth: int = _WRAPPER_SEARCH_DEPTH
    ) -> bool:
        """Whether the node owns a body, following wrappers that delegate theirs.

        Two probes, because grammars disagree on how to express containment:
        field names where they exist, child node types otherwise. Together they
        cover Swift's ``call_suffix`` and Kotlin's ``class_body`` without either
        being enumerated by name.

        Wrappers are followed one level at a time because the body of
        ``export const handler = () => {...}`` hangs off the arrow function, not
        off the statement that declares it.

        ``max_depth`` caps how deep wrappers are followed. Passing 0 checks only
        the node itself, which keeps ``export_statement`` from being treated as
        a declaration when deciding whether to preserve it whole.
        """
        for field in _DECLARATION_BODY_FIELDS:
            if node.child_by_field_name(field) is not None:
                return True
        children = node.named_children
        if any(self._looks_like_body(child) for child in children):
            return True
        if depth >= max_depth or node.type not in _DECLARATION_WRAPPER_TYPES:
            return False
        return any(
            self._carries_body(child, depth + 1, max_depth) for child in children
        )

    @staticmethod
    def _looks_like_body(node: CompatNode) -> bool:
        """Whether a child node is the body of its parent, judged by its type."""
        return node.type in _BODY_NODE_TYPES or node.type.endswith(_BODY_NODE_SUFFIXES)

    def merge_adjacent_windows(
        self, ast_windows: list[list[ASTNode]]
    ) -> Generator[list[ASTNode], None, None]:
        """Merge neighbouring sibling windows while the merged size fits the budget.

        Merging happens here rather than while packing so that only siblings are
        ever joined, which keeps the original tree structure visible in chunks.
        """
        merged_windows = [ast_windows[0][:]]
        for window in ast_windows[1:]:
            current = merged_windows[-1]
            merged_size = sum(n.size for n in current) + sum(n.size for n in window)
            if merged_size <= self.max_chunk_size:
                current.extend(window)
            else:
                merged_windows.append(window[:])
        yield from merged_windows

    def parse(self, code: str) -> CompatNode:
        """Root of ``code``'s syntax tree, reusable across chunking and context."""
        return compat_parse(self.parser, code).root_node

    def chunkify(self, code: str, root: CompatNode | None = None) -> list[AstWindow]:
        """Return the windows of ``code`` in source order.

        ``root`` lets a caller that already parsed the file skip a second parse.
        """
        if root is None:
            root = self.parse(code)
        return [
            AstWindow(
                start_row=window[0].start_row,
                end_row=window[-1].end_row,
                end_column=window[-1].end_column,
            )
            for window in self.assign_tree_to_windows(code, root)
            if window
        ]

    def enclosing_context(
        self, root: CompatNode, start_row: int, end_row: int
    ) -> list[str]:
        """Signature lines of the named declarations strictly enclosing a row span.

        Walks down from the root along the child that contains the span. A
        declaration counts only when it starts above the span: one that starts
        on the span's first row is the chunk's own subject and already visible
        in the text. Unnamed containers (blocks, wrappers, class bodies) are
        transparent, so the chain reads ``class Foo(Base): > def bar(self):``.
        """
        chain: list[str] = []
        node = root
        while True:
            container = None
            for child in node.named_children:
                if child.start_point.row >= start_row:
                    break
                if child.end_point.row >= end_row:
                    container = child
                    break
            if container is None:
                return chain
            if declared_name(container) is not None and self._carries_body(container):
                signature = signature_line(container)
                if signature:
                    chain.append(signature)
            node = container
