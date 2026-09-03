"""cAST chunks carry the enclosing scope chain of their occurrence."""

from oce.domain.chunk import RecursiveChunker
from oce.infrastructure.astchunk.cast_chunker import CastChunker

_PY = '''class Service(Base):
    """Doc."""

    def __init__(self):
        self.items = []
        for i in range(10):
            self.items.append(i * 2)
        self.total = sum(self.items)

    def run(self, x):
        result = []
        for item in self.items:
            if item % 2 == 0:
                result.append(item + x)
            else:
                result.append(item - x)
        return result


def top_level(a, b):
    values = [a, b]
    return sorted(values)
'''


def _chunker(max_chunk_size=80, max_chunk_chars=400):
    return CastChunker(
        max_chunk_size=max_chunk_size,
        fallback=RecursiveChunker(chunk_size=6000),
        max_chunk_chars=max_chunk_chars,
        min_chunk_chars=40,
    )


def test_split_method_body_names_its_class_and_method():
    chunks = _chunker().chunk(_PY, "svc.py")
    contexts = {(c.start_line, c.end_line): c.context for c in chunks}
    assert contexts[(17, 17)] == "class Service(Base): > def run(self, x):"
    # Chunks that start on their own declaration carry no context; the
    # declaration is already visible in the text.
    assert contexts[(20, 22)] is None
    assert chunks[0].context is None


def test_context_is_stored_on_refs_and_embedding_text():
    chunk = next(c for c in _chunker().chunk(_PY, "svc.py") if c.context)
    assert chunk.to_ref().context == chunk.context
    from oce.domain.chunk import LocatedChunk

    located = LocatedChunk(
        "a" * 64,
        chunk.content_hash,
        "svc.py",
        chunk.content,
        chunk.start_line,
        chunk.end_line,
        chunk.context,
    )
    assert located.embedding_text().startswith(
        "File: svc.py\nContext: class Service(Base): > def run(self, x):\n\n"
    )


def test_typescript_class_method_context():
    ts = (
        "export class Store {\n  private count = 0;\n"
        + "".join(
            f"  m{i}(): void {{\n    this.count += {i};\n    this.count += {i};\n  }}\n"
            for i in range(6)
        )
        + "}\n"
    )
    chunks = _chunker(max_chunk_size=60, max_chunk_chars=200).chunk(ts, "s.ts")
    assert len(chunks) > 1
    # The chain names the declaration itself; ``export`` wraps it.
    assert any(c.context == "class Store {" for c in chunks)
