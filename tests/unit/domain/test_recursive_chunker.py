"""RecursiveChunker tests."""

import pytest

from oce.domain.chunk import RecursiveChunker, is_meaningful
from oce.domain.chunk.types import Chunk


class TestRecursiveChunker:
    """Basic behaviour."""

    def test_empty_content(self):
        chunker = RecursiveChunker(chunk_size=1000, chunk_overlap=100)
        assert chunker.chunk("", "test.py") == []

    def test_meaningless_content(self):
        chunker = RecursiveChunker(chunk_size=1000, chunk_overlap=100)
        content = "   \n\n  \t  \n   "
        assert chunker.chunk(content, "test.py") == []

    def test_small_file_single_chunk(self):
        """A small file is one chunk."""
        chunker = RecursiveChunker(chunk_size=1000, chunk_overlap=100)
        content = "def hello():\n    print('world')\n"
        chunks = chunker.chunk(content, "test.py")

        assert len(chunks) == 1
        # trailing blank lines may be dropped
        assert chunks[0].content.strip() == content.strip()
        assert chunks[0].path == "test.py"
        assert chunks[0].chunk_type == "recursive"
        assert chunks[0].start_line == 1
        assert chunks[0].end_line == 2

    def test_chunk_splitting_by_paragraphs(self):
        """Large content splits at paragraph boundaries."""
        chunker = RecursiveChunker(chunk_size=50, chunk_overlap=10)
        # splitting needs enough content
        content = "Line 1 with more content\nLine 2 with more content\n\nLine 3 with more content\nLine 4 with more content\n\nLine 5 with more content\nLine 6 with more content"
        chunks = chunker.chunk(content, "test.txt")

        # paragraph breaks are preferred split points
        assert len(chunks) >= 1
        for chunk in chunks:
            assert isinstance(chunk, Chunk)
            assert chunk.path == "test.txt"
            assert chunk.chunk_type == "recursive"

    def test_single_line_is_not_split(self):
        """A single line is never split: two pieces could not each claim correct lines.

        Splits happen only at line boundaries, so a line longer than chunk_size
        is kept whole rather than cut into pieces that all claim the same line.
        """
        chunker = RecursiveChunker(chunk_size=100, chunk_overlap=20)
        content = "A" * 200
        chunks = chunker.chunk(content, "test.txt")

        assert len(chunks) == 1
        assert chunks[0].content == content
        assert (chunks[0].start_line, chunks[0].end_line) == (1, 1)

    def test_chunks_do_not_overlap(self):
        """Line-numbered chunks never overlap: the same lines would index twice and take two slots."""
        chunker = RecursiveChunker(chunk_size=60, chunk_overlap=20)
        content = "\n".join(f"line {index} carries some content" for index in range(40))
        chunks = chunker.chunk(content, "notes.txt")

        assert len(chunks) > 1
        for previous, current in zip(chunks, chunks[1:], strict=False):
            assert current.start_line > previous.end_line

    def test_python_language_aware(self):
        """Python-specific separators."""
        chunker = RecursiveChunker(chunk_size=100, chunk_overlap=10)
        content = """def func1():
    pass

def func2():
    pass

def func3():
    pass"""
        chunks = chunker.chunk(content, "test.py")

        # Python separators split between functions first.
        assert len(chunks) >= 1
        for chunk in chunks:
            assert "def " in chunk.content

    def test_chunk_hash_uniqueness(self):
        """Different content, different hashes."""
        chunker = RecursiveChunker(chunk_size=1000, chunk_overlap=100)
        chunks1 = chunker.chunk("content A", "test.txt")
        chunks2 = chunker.chunk("content B", "test.txt")

        assert chunks1[0].content_hash != chunks2[0].content_hash

    def test_line_number_accuracy(self):
        """Line numbers are exact."""
        chunker = RecursiveChunker(chunk_size=1000, chunk_overlap=100)
        content = "Line 1\nLine 2\nLine 3\nLine 4\nLine 5"
        chunks = chunker.chunk(content, "test.txt")

        assert len(chunks) == 1
        assert chunks[0].start_line == 1
        assert chunks[0].end_line == 5

    def test_unsupported_language_fallback(self):
        """Unsupported languages use the generic separators."""
        chunker = RecursiveChunker(chunk_size=100, chunk_overlap=10)
        content = "Some random content\n\nMore content"
        chunks = chunker.chunk(content, "unknown.xyz")

        assert len(chunks) >= 1
        assert all(isinstance(c, Chunk) for c in chunks)

    def test_large_file_multiple_chunks(self):
        """A large file is split."""
        chunker = RecursiveChunker(chunk_size=100, chunk_overlap=20)
        content = "\n".join([f"Line {i}" for i in range(100)])
        chunks = chunker.chunk(content, "large.txt")

        assert len(chunks) > 1
        # Every chunk stays near the limit; whole lines may overshoot a little.
        for chunk in chunks:
            assert len(chunk.content) <= 200  # some slack

    @pytest.mark.parametrize(
        "path,content",
        [
            # Indented content: the splitter strips whitespace at separators, and
            # leading indentation was lost so the text no longer matched its lines.
            (
                "styles.css",
                "@media (max-width: 600px) {\n"
                + "\n".join(
                    f"  .item-{index} {{\n    padding: {index}px;\n  }}"
                    for index in range(30)
                )
                + "\n}\n",
            ),
            (
                "data.json",
                "{\n"
                + ",\n".join(f'  "key{index}": "value{index}"' for index in range(60))
                + "\n}\n",
            ),
            (
                "notes.txt",
                "\n\n".join(f"段落 {index} 的正文内容。" for index in range(30)),
            ),
        ],
    )
    def test_chunks_match_their_declared_lines(self, path, content):
        """Chunk text equals the lines it claims; the formatter renders by start_line plus offset."""
        chunker = RecursiveChunker(chunk_size=300, chunk_overlap=20)
        lines = content.splitlines()
        chunks = chunker.chunk(content, path)

        assert chunks
        for chunk in chunks:
            expected = "\n".join(lines[chunk.start_line - 1 : chunk.end_line])
            assert chunk.content == expected, (
                f"{path}#{chunk.start_line}-{chunk.end_line} text differs from the claimed lines"
            )

    def test_overlong_single_line_is_dropped(self):
        """A single line over the hard cap cannot be split with correct line numbers; it is dropped."""
        chunker = RecursiveChunker(chunk_size=300, chunk_overlap=0)
        content = "head line\n" + "z" * 7_000 + "\ntail line\n"
        chunks = chunker.chunk(content, "bundle.min.js")

        assert all(len(chunk.content) <= 6_000 for chunk in chunks)
        assert all("z" * 100 not in chunk.content for chunk in chunks)
        lines = content.splitlines()
        for chunk in chunks:
            assert chunk.content == "\n".join(
                lines[chunk.start_line - 1 : chunk.end_line]
            )


class TestIsMeaningful:
    """is_meaningful."""

    def test_meaningful_content(self):
        assert is_meaningful("hello")
        assert is_meaningful("123")
        assert is_meaningful("  abc  ")
        assert is_meaningful("符号123")

    def test_meaningless_content(self):
        assert not is_meaningful("")
        assert not is_meaningful("   ")
        assert not is_meaningful("\n\n\n")
        assert not is_meaningful(".,;!?")
        assert not is_meaningful("   \t\n   ")
