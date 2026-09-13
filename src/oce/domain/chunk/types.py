"""Chunk values shared by chunkers and metadata repositories."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from oce.shared.hashes import is_sha256_hex


@dataclass
class ChunkRef:
    """A content reference with a 1-based inclusive source span."""

    content_hash: str
    start_line: int
    end_line: int
    # Enclosing scope chain, persisted with blob_chunks: a property of the
    # occurrence, not of the content.
    context: str | None = None

    def __post_init__(self) -> None:
        if not is_sha256_hex(self.content_hash):
            raise ValueError(f"Invalid content_hash: {self.content_hash}")
        if self.start_line < 1:
            raise ValueError(f"Invalid start_line: {self.start_line}")
        if self.end_line < self.start_line:
            raise ValueError(f"Invalid line range: {self.start_line} - {self.end_line}")


@dataclass
class Chunk:
    """A content-addressed code chunk with a 1-based inclusive span.

    ``content`` is verbatim source text for the reported span, so it renders
    correctly at the line numbers the formatter prints.
    """

    content_hash: str
    path: str
    content: str
    start_line: int
    end_line: int
    chunk_type: str | None = None
    # Enclosing scope chain from the chunker (``class Foo(Base) > def bar(self)``).
    # Not part of content_hash: the same text may sit in different scopes in
    # different files.
    context: str | None = None

    def __post_init__(self) -> None:
        if not is_sha256_hex(self.content_hash):
            raise ValueError(f"Invalid content_hash: {self.content_hash}")
        if self.start_line < 1 or self.end_line < self.start_line:
            raise ValueError(f"Invalid line range: {self.start_line} - {self.end_line}")

    @staticmethod
    def compute_hash(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def to_ref(self) -> ChunkRef:
        return ChunkRef(
            self.content_hash, self.start_line, self.end_line, context=self.context
        )


@dataclass(frozen=True)
class LocatedChunk:
    """A persisted chunk occurrence ready to be written to the vector index."""

    blob_name: str
    content_hash: str
    path: str
    content: str
    start_line: int
    end_line: int
    context: str | None = None

    @property
    def chunk_id(self) -> str:
        raw = (
            f"{self.blob_name}\n{self.content_hash}\n{self.start_line}:{self.end_line}"
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def embedding_text(self) -> str:
        """Contextualized embedding input: path, enclosing scope, then the code.

        A method cut out of its class carries no class name in its body; the
        header restores that so the vector answers "which Foo.bar" instead of
        "some bar". Bump EMBEDDING_PIPELINE_VERSION when this shape changes.
        """
        header = f"File: {self.path}"
        if self.context:
            header += f"\nContext: {self.context}"
        return f"{header}\n\n{self.content}"
