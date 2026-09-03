"""Lexical tokenization shared by the chunk term index and query-side matching.

Both sides must agree on token shapes, so the document and the query go
through the same function. Identifiers are emitted twice: once as a joined
surrogate (``parse_config`` → ``parseconfig``) that only matches the whole
identifier, and once split into sub-words (``parse``, ``config``) so
``ParseConfig``, ``parse_config`` and "parse the config" all meet. Every
emitted token is lowercase alphanumeric, which keeps SQLite FTS5 (unicode61)
and PostgreSQL ``simple`` tokenization from re-splitting anything.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from oce.domain.chunk import Chunk

_TOKEN = re.compile(r"[A-Za-z0-9_]+")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_ALNUM_BOUNDARY = re.compile(r"(?<=[A-Za-z])(?=[0-9])|(?<=[0-9])(?=[A-Za-z])")

# Words too common in either natural language or source code to discriminate.
# Programming keywords stay out of the index on purpose: ``return`` and ``self``
# occur in nearly every chunk and would only inflate OR-query candidate sets.
STOPWORDS = frozenset(
    """
    a an the and or not but if else elif then when where which what who whom
    whose why how this that these those there here is are was were be been being
    am do does did doing done have has had having can could should would will
    shall may might must of in on at to from by for with without into onto over
    under about above below between among through during before after while
    until as so than too very just also only even still yet already again
    it its it's i me my we our you your he she they them their his her
    all any some each every either neither both few more most other another
    such same different own same no nor none nothing
    use used using uses want wants need needs like get got make made
    please help find show me tell give see look check know
    code file files function functions method methods class classes module
    implement implemented implementation logic feature where defined definition
    return returns self cls none true false null nil void var let const
    def fn func function import from export package public private static
    new delete this super async await try catch except finally raise throw
    int str string bool float list dict set tuple object type
    """.split()
)

MIN_TOKEN_CHARS = 2


def split_identifier(token: str) -> list[str]:
    """Sub-words of one identifier, lowercase, in order."""
    parts: list[str] = []
    for piece in token.split("_"):
        if not piece:
            continue
        for camel in _CAMEL_BOUNDARY.split(piece):
            for part in _ALNUM_BOUNDARY.split(camel):
                if part:
                    parts.append(part.lower())
    return parts


def lexical_tokens(text: str) -> list[str]:
    """Tokens for indexing or matching; see the module docstring for the shape."""
    tokens: list[str] = []
    for match in _TOKEN.finditer(text):
        raw = match.group()
        parts = split_identifier(raw)
        if not parts:
            continue
        if len(parts) > 1:
            surrogate = "".join(parts)
            if len(surrogate) >= MIN_TOKEN_CHARS:
                tokens.append(surrogate)
        tokens.extend(part for part in parts if len(part) >= MIN_TOKEN_CHARS)
    return tokens


def build_lexical_document(content: str, *, max_tokens: int = 4_000) -> str:
    """Space-joined tokens of a chunk; ``max_tokens`` bounds index size."""
    return " ".join(lexical_tokens(content)[:max_tokens])


def query_terms(
    text: str,
    *,
    limit: int = 24,
    extra: Iterable[str] = (),
) -> tuple[str, ...]:
    """Distinct, stopword-free tokens for an OR query, most specific first.

    Longer and multi-part tokens are more selective, so they are kept ahead of
    short generic words when the limit trims the list. ``extra`` tokens (for
    example identifiers recovered from a traceback) are always kept.
    """
    seen: dict[str, int] = {}

    def add(token: str, weight: int) -> None:
        if token in STOPWORDS or len(token) < MIN_TOKEN_CHARS:
            return
        if token.isdigit() and len(token) < 3:
            return
        seen[token] = max(seen.get(token, 0), weight)

    for token in lexical_tokens(" ".join(extra)):
        add(token, 100 + len(token))
    for token in lexical_tokens(text):
        add(token, len(token))

    ordered = sorted(seen, key=lambda token: (-seen[token], token))
    return tuple(ordered[:limit])


def phrase_tokens(phrase: str) -> tuple[str, ...]:
    """Ordered tokens of a quoted phrase; empty when nothing indexable remains."""
    return tuple(lexical_tokens(phrase))


class LexicalProjection(Protocol):
    """Persist term documents when a blob's immutable chunks are created."""

    async def index(self, chunks: Sequence[Chunk]) -> None: ...
