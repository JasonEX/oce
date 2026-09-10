"""Whole query entities; identifying a name does not decide the requested evidence."""

from __future__ import annotations

import re

from oce.domain.chunk.lang import detect_language

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
        lambda match: " " * len(match.group())
        if is_probable_filename(match.group())
        else match.group(),
        text,
    )


PATH_TOKEN_PATTERN = re.compile(r"(?:[A-Za-z0-9_.\-]+[/\\])+[A-Za-z0-9_.\-]+")
_IDENTIFIER = re.compile(
    r"[A-Za-z_$][A-Za-z0-9_$]*(?:(?:::|\.)[A-Za-z_$][A-Za-z0-9_$]*)*"
)
_TOKEN = re.compile(r"(?<![A-Za-z0-9_$])" + _IDENTIFIER.pattern + r"(?![A-Za-z0-9_$])")
_TYPE_AFTER = re.compile(
    r"\s*(?:的)?(?:前后端)?(?:类型|类|接口|结构|定义|(?:type|interface|struct|enum|trait|class|definition|defined|implemented)\b)",
    re.I,
)
_URL = re.compile(r"[a-z][a-z0-9+.\-]*://\S+")
_DOMAIN_SUFFIXES = frozenset({"com", "org", "net", "io", "dev", "edu", "gov"})

_NAME_BEFORE = re.compile(
    r"(?i)(?:definition|declaration|callers?|references?|usages?|uses|tests?)\s+(?:of|to|for|covering|exercising)\s*$|\b(?:calls?|invokes?)\s*$"
)
_NAME_AFTER = re.compile(
    r"(?i)\s*(?:(?:function|method|type|class)\s+)?(?:defined|definition|declaration|used|called|referenced)\b|\s*(?:的)?(?:定义|声明|被使用|被调用)"
)
_PROSE_NAMES = frozenset(
    "a an the is are function method class type definition declaration caller callers test tests of for to it this its how".split()
)


def mask_code(query: str, identifiers: tuple[str, ...]) -> str:
    """Keep offsets while hiding code so verbs inside names cannot steer routing."""
    text = _URL.sub(lambda m: " " * len(m.group()), query)
    text = PATH_TOKEN_PATTERN.sub(lambda m: " " * len(m.group()), text)
    text = mask_filenames(text)
    for name in sorted(identifiers, key=len, reverse=True):
        text = re.sub(
            r"(?<![A-Za-z0-9_$])" + re.escape(name) + r"(?![A-Za-z0-9_$])",
            lambda m: " " * len(m.group()),
            text,
        )
    return re.sub(r"`[^`]+`", lambda m: " " * len(m.group()), text)


def extract_code_identifiers(query: str) -> tuple[str, ...]:
    """Read names in text order, preserving qualification and private spelling.

    Explicit quoting accepts plain names. Outside quotes, compound spelling,
    call syntax or a type/declaration noun supplies code evidence. Paths and
    URLs are masked before scanning, and matches always cover whole tokens.
    """
    text = _URL.sub(lambda m: " " * len(m.group()), query)
    text = PATH_TOKEN_PATTERN.sub(lambda m: " " * len(m.group()), text)
    text = mask_filenames(text)
    quoted = {
        m.span(1)
        for m in re.finditer(r"`([^`]+)`", text)
        if _IDENTIFIER.fullmatch(m.group(1))
    }
    found: list[str] = []
    for match in _TOKEN.finditer(text):
        name = match.group()
        qualified = "::" in name or (
            "." in name and name.rsplit(".", 1)[-1] not in _DOMAIN_SUFFIXES
        )
        shaped = "_" in name or bool(re.search(r"[a-z][A-Z]|[A-Z]{2}[a-z]", name))
        typed = name[0].isupper() and bool(_TYPE_AFTER.match(text, match.end()))
        called = text[match.end() :].startswith("(")
        requested = name.lower() not in _PROSE_NAMES and bool(
            _NAME_BEFORE.search(text[: match.start()])
            or (
                _NAME_AFTER.match(text, match.end())
                and (
                    not text[: match.start()].strip()
                    or re.search(
                        r"(?i)\bwhere\s+(?:is|are)\s+(?:the\s+)?$|(?:找到|查找|定位)\s*$",
                        text[: match.start()],
                    )
                )
            )
        )
        if match.span() not in quoted and not (
            qualified or shaped or typed or called or requested
        ):
            continue
        if "." in name and not qualified:
            continue
        if name not in found:
            found.append(name)
    # A repeated leaf and its qualified spelling are the same lookup target.
    return tuple(
        name
        for name in found
        if not any(
            other.endswith(("." + name, "::" + name))
            for other in found
            if other != name
        )
    )
