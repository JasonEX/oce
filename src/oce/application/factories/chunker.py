"""Chunker 装配:把 infrastructure 的各语言 chunker 组装进 router。"""

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
    """构建 Chunker router，使用 RecursiveChunker 作为统一 fallback。

    架构说明：
    - RecursiveChunker: 基于 LangChain，智能递归分隔，支持语言特定规则
    - CastChunker: AST 语义切块，内部自带 RecursiveCharacterTextSplitter fallback
    - 各专用 chunker (Markdown/JSP/Vue): 针对特定格式优化
    - FixedChunker 已废弃，完全由 RecursiveChunker 替代
    """
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
                chunk_overlap=0,
                fallback=recursive_chunker,
            ),
            MarkdownChunker(fallback=recursive_chunker),
            JspChunker(fallback=recursive_chunker),
            VueChunker(fallback=recursive_chunker),
        ),
    )
