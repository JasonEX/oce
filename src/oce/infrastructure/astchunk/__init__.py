"""AST-aware source chunking infrastructure."""

from .astchunk import ASTChunk as ASTChunk
from .astchunk_builder import ASTChunkBuilder as ASTChunkBuilder
from .astchunk_builder import LANGUAGE_MAP as LANGUAGE_MAP
from .astchunk_builder import get_supported_languages as get_supported_languages
from .astnode import ASTNode as ASTNode
from .preprocessing import (
    ByteRange as ByteRange,
    IntRange as IntRange,
    get_largest_node_in_brange as get_largest_node_in_brange,
    get_nodes_in_brange as get_nodes_in_brange,
    get_nws_count as get_nws_count,
    get_nws_count_direct as get_nws_count_direct,
    preprocess_nws_count as preprocess_nws_count,
)

__version__ = "0.1.0"
