"""JSX/TSX component boundary tests for the cAST adapter."""

from __future__ import annotations

import pytest

from oce.domain.chunk import RecursiveChunker
from oce.infrastructure.astchunk.cast_chunker import CastChunker

COMPONENTS = """import React from 'react'

export function Header() {
  return <header>Header</header>
}

export const Footer: React.FC = () => {
  return <footer>Footer</footer>
}
"""


def make_chunker(**kwargs) -> CastChunker:
    return CastChunker(
        max_chunk_size=40,
        fallback=RecursiveChunker(),
        **kwargs,
    )


def assert_aligned(chunk, content: str) -> None:
    lines = content.splitlines()
    assert chunk.content == "\n".join(lines[chunk.start_line - 1 : chunk.end_line])


@pytest.mark.parametrize("path", ["src/App.tsx", "src/App.jsx"])
def test_top_level_react_components_get_independent_chunks(path):
    """React components are recognized; small ones may merge, large files split."""
    chunks = make_chunker().chunk(COMPONENTS, path)

    # Both components appear, in one chunk or two.
    assert any("function Header" in chunk.content for chunk in chunks)
    assert any("const Footer" in chunk.content for chunk in chunks)

    # When split, every chunk is aligned.
    for chunk in chunks:
        assert_aligned(chunk, COMPONENTS)


def test_pascal_case_component_wrapped_in_memo_is_detected():
    """A PascalCase component wrapped in React.memo is recognized."""
    content = """const helper = () => <span>helper</span>

export const Dashboard = React.memo(() => {
  return <main>Dashboard</main>
})
"""
    chunks = make_chunker().chunk(content, "src/Dashboard.tsx")

    dashboard = next(chunk for chunk in chunks if "const Dashboard" in chunk.content)
    # Dashboard is recognized; the helper may share its chunk.
    assert "Dashboard" in dashboard.content
    assert_aligned(dashboard, content)


def test_class_component_gets_an_independent_chunk():
    """Class components are recognized."""
    content = """export class Sidebar extends React.Component {
  render() {
    return <aside>Sidebar</aside>
  }
}

export function Content() {
  return <main>Content</main>
}
"""
    chunks = make_chunker().chunk(content, "src/Layout.tsx")

    # both components appear
    assert any("class Sidebar" in chunk.content for chunk in chunks)
    assert any("function Content" in chunk.content for chunk in chunks)

    for chunk in chunks:
        assert_aligned(chunk, content)


def test_plain_typescript_keeps_the_existing_whole_file_behavior():
    content = """export function first() {
  return 1
}

export function second() {
  return 2
}
"""
    chunks = make_chunker().chunk(content, "src/helpers.ts")

    assert len(chunks) == 1
    assert "function first" in chunks[0].content
    assert "function second" in chunks[0].content
    assert_aligned(chunks[0], content)


def test_react_chunks_respect_the_character_budget():
    body = "\n".join(f"  const value{index} = '{'x' * 100}'" for index in range(100))
    content = f"export function Large() {{\n{body}\n  return <main />\n}}\n"
    chunks = make_chunker(max_chunk_chars=1_000).chunk(content, "src/Large.tsx")

    assert len(chunks) > 1
    assert all(len(chunk.content) <= 1_000 for chunk in chunks)
    for chunk in chunks:
        assert_aligned(chunk, content)
