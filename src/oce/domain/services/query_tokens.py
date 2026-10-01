"""Shapes in request text: code identifiers, file names and paths.

Routing, evidence extraction and lexical recall all need to tell a code symbol
from a file name and a path from prose. The shapes are defined once here so the
intent classifier and the evidence extractor read the same request the same
way.
"""

from __future__ import annotations

import re

from oce.domain.chunk.lang import detect_language

_IDENTIFIER_PATTERN = re.compile(
    r"^[A-Za-z_$][A-Za-z0-9_$]*(?:::[A-Za-z_$][A-Za-z0-9_$]*)*$"
)
# Whole identifiers only: ``__init__`` must not yield a fragment such as
# ``init__``, and private names keep their leading underscores as spelled.
_SNAKE_IDENTIFIER_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])_*[a-z][a-z0-9]*_[a-z0-9_]+(?![A-Za-z0-9_])"
)
_QUALIFIED_IDENTIFIER_PATTERN = re.compile(
    r"[A-Za-z_$][A-Za-z0-9_$]*(?:::[A-Za-z_$][A-Za-z0-9_$]*)+"
)
# A capitalised name followed by a type noun ("Provider 类型", "User class").
# Chinese may put a short modifier between the possessive and the noun
# ("Provider 的前后端类型定义"); without the possessive no modifier is
# allowed, so "这类" in "Python 这类问题" does not make a type of Python.
_TYPE_IDENTIFIER_PATTERN = re.compile(
    r"([A-Z][A-Za-z0-9_$]*)\s*(?:的[\u4e00-\u9fff]{0,4}?)?"
    r"(?:类型|类|接口|结构|定义|"
    r"(?:type|interface|struct|enum|trait|class|definition|defined|implemented)\b)"
)
_CONSTANT_IDENTIFIER_PATTERN = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")

# A token with a file extension (config.json, lib.rs) is strong evidence of
# a file request. The extension must start with a letter so a version such
# as 3.13 is not a file name. Candidates are filtered by the supported
# languages and common project extensions: length alone would drop
# ``build.csproj`` and ``application.properties``, and no filter would take
# ``Session.request`` for a file.
FILENAME_TOKEN_PATTERN = re.compile(
    r"[A-Za-z0-9_\-]+\.[A-Za-z][A-Za-z0-9]{0,15}(?![A-Za-z0-9_])"
)
_EXTRA_FILE_SUFFIXES = frozenset(
    {
        ".adoc",
        ".cfg",
        ".csv",
        ".csproj",
        ".env",
        ".fsproj",
        ".gradle",
        ".ini",
        ".lock",
        ".properties",
        ".props",
        ".proto",
        ".rst",
        ".sln",
        ".targets",
        ".tf",
        ".txt",
        ".vbproj",
    }
)


def is_probable_filename(token: str) -> bool:
    suffix = "." + token.rsplit(".", 1)[-1].lower()
    return detect_language(token) is not None or suffix in _EXTRA_FILE_SUFFIXES


def has_filename(text: str) -> bool:
    return any(
        is_probable_filename(match.group())
        for match in FILENAME_TOKEN_PATTERN.finditer(text)
    )


def mask_filenames(text: str) -> str:
    return FILENAME_TOKEN_PATTERN.sub(
        lambda match: " " if is_probable_filename(match.group()) else match.group(),
        text,
    )


# A dotted qualified name (``Context.ShouldBindJSON``,
# ``requests.Session.request``) is a code symbol only when its last segment
# is CamelCase or snake_case; ``example.com`` and ``Foo.bar`` are not.
_DOTTED_IDENTIFIER_PATTERN = re.compile(
    r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*"
    r"\.((?:[A-Z][a-z0-9]+(?:[A-Z][A-Za-z0-9]*)+)|(?:[a-z]+_[a-z0-9_]+)|(?:[A-Z][A-Z0-9]+[a-z][A-Za-z0-9]*))\b"
)
# Paths are masked before snake_case identifiers are read; otherwise
# ``src/message_definition.py`` would yield a ``message_definition`` symbol
# and ``__init__.py`` an ``init__`` one.
PATH_TOKEN_PATTERN = re.compile(r"(?:[A-Za-z0-9_.\-]+[/\\])+[A-Za-z0-9_.\-]+")

# ``Session.get`` / ``binding.Default``: identifier segments joined by dots. The
# qualifier is kept because it disambiguates same-named declarations; the
# retrieval pipeline derives the leaf to look up.
_DOTTED_QUALIFIED_PATTERN = re.compile(
    r"^[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)+$"
)


def extract_code_identifiers(query: str) -> tuple[str, ...]:
    """Code identifiers suitable for exact recall, in order of appearance.

    A qualified name (``Session.get``, ``a::b::C``) stays one identifier: the
    qualifier is disambiguating evidence and the pipeline derives the leaf,
    otherwise one qualified name would count as two symbols.
    """
    identifiers: list[str] = []

    def add(value: str) -> None:
        value = value.strip()
        if not (
            _IDENTIFIER_PATTERN.fullmatch(value)
            or _DOTTED_QUALIFIED_PATTERN.fullmatch(value)
        ):
            return
        # The leaf of a qualified name already listed is the same symbol.
        # Leading underscores are significant: ``_load_config`` and
        # ``load_config`` may both exist in the same scope.
        if value in identifiers or any(
            item.endswith((f".{value}", f"::{value}")) for item in identifiers
        ):
            return
        identifiers.append(value)

    for value in re.findall(r"`([^`]+)`", query):
        add(value)

    # Backticked names are read from the raw text; the heuristic scan masks
    # paths and file names so a file name is not exact-symbol evidence.
    # Identifiers outside paths, such as ``load_config``, are unaffected.
    identifier_text = PATH_TOKEN_PATTERN.sub(" ", query)
    identifier_text = mask_filenames(identifier_text)
    for pattern in (
        _QUALIFIED_IDENTIFIER_PATTERN,
        _DOTTED_IDENTIFIER_PATTERN,
        _SNAKE_IDENTIFIER_PATTERN,
        _CONSTANT_IDENTIFIER_PATTERN,
        _TYPE_IDENTIFIER_PATTERN,
    ):
        for match in pattern.finditer(identifier_text):
            if pattern is _DOTTED_IDENTIFIER_PATTERN:
                # The whole qualified spelling, unless its leaf was already
                # named on its own (``\`get\`` and ``Session.get`` in one
                # request describe one symbol).
                if match.group(1) not in identifiers:
                    add(match.group())
                continue
            add(match.group(1) if match.lastindex else match.group())

    return tuple(identifiers)
