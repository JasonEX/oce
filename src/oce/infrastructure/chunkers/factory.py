"""Production composition for language-specific chunkers."""

from oce.domain.chunk import Chunker, LanguageChunkerRouter
from oce.domain.chunk.recursive_chunker import RecursiveChunker
from oce.infrastructure.astchunk.cast_chunker import CastChunker
from oce.infrastructure.chunkers.jsp_chunker import JspChunker
from oce.infrastructure.chunkers.markdown_chunker import MarkdownChunker
from oce.infrastructure.chunkers.vue_chunker import VueChunker


def build_chunker(
    *,
    semantic_enabled: bool = True,
    semantic_max_chunk_chars: int = 1500,
    recursive_chunk_size: int = 6000,
    recursive_chunk_overlap: int = 200,
) -> Chunker:
    """Build the production router with recursive chunking as its fallback."""
    recursive_chunker = RecursiveChunker(
        chunk_size=recursive_chunk_size,
        chunk_overlap=recursive_chunk_overlap,
    )
    if not semantic_enabled:
        return recursive_chunker

    return LanguageChunkerRouter(
        fallback=recursive_chunker,
        language_chunkers=(
            CastChunker(
                max_chunk_size=semantic_max_chunk_chars,
                fallback=recursive_chunker,
            ),
            MarkdownChunker(fallback=recursive_chunker),
            JspChunker(fallback=recursive_chunker),
            VueChunker(fallback=recursive_chunker),
        ),
    )
