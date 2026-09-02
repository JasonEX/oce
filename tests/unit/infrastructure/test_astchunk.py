"""ASTChunkBuilder window assignment."""

from __future__ import annotations

import pytest

from oce.infrastructure.astchunk.astchunk_builder import ASTChunkBuilder, AstWindow

PYTHON_CLASS = """class Calculator:
    def add(self, a, b):
        return a + b

    def multiply(self, a, b):
        return a * b
"""

SAMPLES = [
    ("python", PYTHON_CLASS),
    (
        "java",
        """public class Calculator {
    public int add(int a, int b) {
        return a + b;
    }
}""",
    ),
    (
        "javascript",
        """class Calculator {
    add(a, b) {
        return a + b;
    }
}""",
    ),
    (
        "typescript",
        """class Calculator {
    add(a: number, b: number): number {
        return a + b;
    }
}""",
    ),
]


def _builder(language: str, max_chunk_size: int) -> ASTChunkBuilder:
    return ASTChunkBuilder(max_chunk_size=max_chunk_size, language=language)


@pytest.mark.parametrize("language,code", SAMPLES)
def test_windows_cover_source_in_order(language: str, code: str) -> None:
    windows = _builder(language, 100).chunkify(code)

    assert windows
    assert windows[0].start_row == 0
    # A tree that ends on a line break reports the row after its last token.
    assert windows[-1].end_row >= code.rstrip("\n").count("\n")
    for previous, current in zip(windows, windows[1:], strict=False):
        assert current.start_row >= previous.start_row
        assert current.end_row >= previous.end_row


def test_small_tree_is_one_window() -> None:
    windows = _builder("python", 500).chunkify("x = 1")

    assert windows == [AstWindow(start_row=0, end_row=0, end_column=5)]


def test_windows_respect_non_whitespace_budget() -> None:
    code = "\n".join(f"x{i} = {i}" for i in range(100))
    builder = _builder("python", 200)
    lines = code.splitlines()

    for window in builder.chunkify(code):
        text = "\n".join(lines[window.start_row : window.end_row + 1])
        assert sum(1 for char in text if not char.isspace()) <= builder.max_chunk_size


@pytest.mark.parametrize("code", ["", "   \n\n   \n"])
def test_blank_source_yields_no_window(code: str) -> None:
    assert _builder("python", 500).chunkify(code) == []


def test_large_single_node_does_not_crash() -> None:
    code = "data = [\n" + ",\n".join(f"    {i}" for i in range(5000)) + "\n]\n"

    windows = _builder("python", 500).chunkify(code)

    assert windows
    assert windows[0].start_row == 0


def test_unknown_language_raises() -> None:
    with pytest.raises(ValueError, match="No tree-sitter grammar"):
        ASTChunkBuilder(max_chunk_size=100, language="pascal")
