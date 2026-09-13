"""In-memory write-path doubles: the vector index and a constant embedder."""

from __future__ import annotations

from oce.domain.services.search import VectorRecord


class ConstantEmbedder:
    """Every text embeds to the same unit vector; enough to drive indexing."""

    def __init__(self, dimensions: int = 4) -> None:
        self.dimensions = dimensions
        self.calls: list[list[str]] = []

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[1.0] * self.dimensions for _ in texts]

    async def embed_query(self, text: str) -> list[float]:
        return [1.0] * self.dimensions


class FakeSymbolProjection:
    """Records ``(blob_name, chunk hashes)`` per indexed blob."""

    def __init__(self) -> None:
        self.indexed: list[tuple[str, tuple[str, ...]]] = []

    async def index(self, blob, chunks, content: str = "") -> None:
        self.indexed.append(
            (blob.blob_name, tuple(chunk.content_hash for chunk in chunks))
        )


class FakeLexicalProjection:
    """Records the chunk hashes handed to the term index."""

    def __init__(self) -> None:
        self.indexed: list[tuple[str, ...]] = []

    async def index(self, chunks) -> None:
        self.indexed.append(tuple(chunk.content_hash for chunk in chunks))


class RecordingVectorIndex:
    """``VectorIndex`` that keeps every upserted record in insertion order."""

    def __init__(self) -> None:
        self.records: list[VectorRecord] = []
        self.deleted: list[str] = []

    async def upsert(self, records) -> None:
        self.records.extend(records)

    async def delete(self, blob_names) -> None:
        self.deleted.extend(blob_names)
        names = set(blob_names)
        self.records = [
            record for record in self.records if record.blob_name not in names
        ]
