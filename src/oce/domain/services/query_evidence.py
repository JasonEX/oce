"""Deterministic evidence recovered from the request text.

Issue-style requests carry more than a question: traceback frames name the
files and functions on the failing path, quoted error text is a literal that
appears in the source, and explicit filenames point at files directly. Each
piece feeds a different recall operator, so they are extracted once here and
handed to the pipeline as one value.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from oce.domain.services.lexical import query_terms
from oce.domain.services.query_symbols import (
    FILENAME_TOKEN_PATTERN,
    PATH_TOKEN_PATTERN,
    extract_code_identifiers,
    is_probable_filename,
)

# Python: ``File "/site-packages/pkg/mod.py", line 12, in handler``
_PY_FRAME = re.compile(r'File "([^"\n]+)", line (\d+), in ([A-Za-z_<][\w>]*)')
# IPython: ``File ~/env/lib/python3.9/site-packages/xarray/core/dataset.py:2110, in Dataset.chunks(self)``
_IPY_FRAME = re.compile(r"File ([^\s\"':]+\.py):(\d+), in ([A-Za-z_][\w.]*)")
# Node/JS: ``at handler (/app/src/server.ts:12:5)`` or ``at /app/src/server.ts:12:5``
_JS_FRAME = re.compile(
    r"\bat (?:(?:async|new) )?(?:([A-Za-z_$][\w$.<>]*) \()?"
    r"((?:[A-Za-z]:)?[^\s():]+\.[cm]?[jt]sx?):(\d+):\d+\)?"
)
# ``path/to/file.rs:42`` style locations (Rust, Go, compilers, linters).
_LOCATION = re.compile(r"((?:[\w.\-]+[/\\])+[\w.\-]+\.[A-Za-z][A-Za-z0-9]{0,7}):\d+\b")
# Terminal error line of a traceback: ``ValueError: invalid literal for int()``
_ERROR_LINE = re.compile(
    r"^\s*(?:[\w.]+\.)?([A-Z][A-Za-z0-9]*(?:Error|Exception|Warning|Fault))"
    r"\s*:\s*(.+?)\s*$",
    re.MULTILINE,
)
_QUOTED = re.compile(r'"([^"\n]{4,160})"|\'([^\'\n]{4,160})\'|`([^`\n]{4,160})`')
_URL = re.compile(r"[a-z][a-z0-9+.\-]*://\S+")
_SITE_PACKAGES = re.compile(r".*?(?:site-packages|dist-packages)[/\\]")
_NOISE_FRAMES = frozenset({"<module>", "<lambda>", "<listcomp>", "<genexpr>"})

MAX_PHRASE_CHARS = 120


@dataclass(frozen=True)
class QueryFrame:
    """One traceback frame: the file, the function and the line it was in."""

    path: str
    function: str
    line: int | None = None


@dataclass(frozen=True)
class QueryEvidence:
    """Structured recall inputs derived from one request."""

    identifiers: tuple[str, ...]
    filenames: tuple[str, ...]
    paths: tuple[str, ...]
    phrases: tuple[str, ...]
    terms: tuple[str, ...]
    # Traceback frames in call order, outermost first; a frame ties a
    # function to the file that declares it and the line it was in.
    frames: tuple[QueryFrame, ...] = ()

    @property
    def has_path_evidence(self) -> bool:
        return bool(self.filenames or self.paths)


def _relative_path(raw: str) -> str:
    """Trim absolute prefixes so the suffix matcher sees repository-relative text."""
    path = raw.replace("\\", "/").strip()
    stripped = _SITE_PACKAGES.sub("", path)
    if stripped != path:
        return stripped
    return path.lstrip("/")


def _add(target: list[str], value: str) -> None:
    if value and value not in target:
        target.append(value)


def extract_query_evidence(query: str) -> QueryEvidence:
    identifiers = list(extract_code_identifiers(query))
    filenames: list[str] = []
    paths: list[str] = []
    phrases: list[str] = []

    text = _URL.sub(" ", query)
    frames: list[QueryFrame] = []

    def add_frame(raw_path: str, function: str, line: str | None) -> None:
        path = _relative_path(raw_path)
        _add(paths, path)
        leaf = function.rsplit(".", 1)[-1]
        if leaf in _NOISE_FRAMES or leaf.startswith("<"):
            return
        _add(identifiers, leaf)
        frame = QueryFrame(path, leaf, int(line) if line else None)
        if all((item.path, item.function) != (path, leaf) for item in frames):
            frames.append(frame)

    for match in _PY_FRAME.finditer(text):
        add_frame(match.group(1), match.group(3), match.group(2))
    for match in _IPY_FRAME.finditer(text):
        add_frame(match.group(1), match.group(3), match.group(2))
    for match in _JS_FRAME.finditer(text):
        if match.group(1):
            add_frame(match.group(2), match.group(1), match.group(3))
        else:
            _add(paths, _relative_path(match.group(2)))
    for match in _LOCATION.finditer(text):
        _add(paths, _relative_path(match.group(1)))

    for match in _ERROR_LINE.finditer(text):
        _add(identifiers, match.group(1))
        message = match.group(2)[:MAX_PHRASE_CHARS]
        if len(message.split()) >= 2:
            _add(phrases, message)

    for match in _QUOTED.finditer(text):
        quoted = next(group for group in match.groups() if group is not None).strip()
        if len(quoted.split()) < 2 or PATH_TOKEN_PATTERN.fullmatch(quoted):
            continue
        _add(phrases, quoted[:MAX_PHRASE_CHARS])

    frame_paths = set(paths)
    for match in PATH_TOKEN_PATTERN.finditer(text):
        token = match.group().strip("/\\")
        if "." in token.rsplit("/", 1)[-1] and token not in frame_paths:
            _add(paths, _relative_path(token))
    for match in FILENAME_TOKEN_PATTERN.finditer(text):
        token = match.group()
        if is_probable_filename(token):
            _add(filenames, token)
    for path in paths:
        name = path.rsplit("/", 1)[-1]
        if is_probable_filename(name):
            _add(filenames, name)

    terms = query_terms(
        PATH_TOKEN_PATTERN.sub(" ", text),
        extra=[*identifiers, *(name.rsplit(".", 1)[0] for name in filenames)],
    )
    return QueryEvidence(
        identifiers=tuple(identifiers),
        filenames=tuple(filenames),
        paths=tuple(paths),
        phrases=tuple(phrases),
        terms=terms,
        frames=tuple(frames),
    )
