"""Language-aware chunker dispatch."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import MappingProxyType

from oce.domain.chunk.lang import SUPPORTED_LANGUAGES, detect_language
from oce.domain.chunk.protocols import Chunker, LanguageChunker
from oce.domain.chunk.types import Chunk


class LanguageChunkerRouter:
    """Route detected languages to self-declaring chunker implementations."""

    def __init__(
        self,
        *,
        language_chunkers: Iterable[LanguageChunker],
        fallback: Chunker,
    ) -> None:
        self.language_chunkers = self._register(language_chunkers)
        self.fallback = fallback

    def chunk(self, content: str, path: str) -> list[Chunk]:
        language = detect_language(path)
        chunker = self.language_chunkers.get(language) if language is not None else None
        if chunker is None:
            return self.fallback.chunk(content, path)
        return chunker.chunk(content, path)

    @staticmethod
    def _register(
        chunkers: Iterable[LanguageChunker],
    ) -> Mapping[str, LanguageChunker]:
        registered: dict[str, LanguageChunker] = {}
        for chunker in chunkers:
            if not isinstance(chunker, LanguageChunker):
                raise TypeError("language_chunkers must implement LanguageChunker")
            if not isinstance(chunker.languages, frozenset):
                raise TypeError("LanguageChunker.languages must be a frozenset")
            if not chunker.languages:
                raise ValueError("LanguageChunker.languages must not be empty")
            for language in chunker.languages:
                if (
                    not isinstance(language, str)
                    or language != language.strip().lower()
                ):
                    raise ValueError(
                        f"LanguageChunker language must be a normalized string: {language!r}"
                    )
                if language not in SUPPORTED_LANGUAGES:
                    raise ValueError(
                        f"LanguageChunker declares an unknown language: {language}"
                    )
                if language in registered:
                    raise ValueError(
                        f"LanguageChunker language registered twice: {language}"
                    )
                registered[language] = chunker
        return MappingProxyType(registered)
