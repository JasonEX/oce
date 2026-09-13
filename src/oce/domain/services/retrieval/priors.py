"""Static path priors: what kind of file a hit lives in.

The factor multiplies a hit's fused score before the head rules run. It is
deliberately coarse: it separates implementation from documentation, tests,
vendored code and configuration, and never tries to rank two source files.
"""

from __future__ import annotations

import re

from oce.domain.chunk.lang import detect_language
from oce.domain.services.test_paths import is_test_path

_DOCUMENT_DIRECTORIES = frozenset(
    {
        "docs",
        "doc",
        "examples",
        "example",
        "samples",
        "sample",
        "changelog",
        "changelogs",
        "news",
    }
)
_VENDORED_DIRECTORIES = frozenset(
    {"vendor", "vendored", "_vendor", "third_party", "thirdparty", "node_modules"}
)
_DOCUMENT_STEMS = frozenset(
    {"changelog", "changes", "history", "news", "authors", "contributors", "todo"}
)
_PRIMARY_README_NAMES = frozenset(
    {"readme", "readme.md", "readme.rst", "readme.txt", "readme.adoc"}
)
_CONFIG_SUFFIXES = (".cfg", ".ini", ".toml", ".yaml", ".yml", ".json")
# .coveragerc, .pylintrc, pylintrc, tox.ini-style rc files
_RC_FILE = re.compile(r"^\.?[a-z0-9_-]+rc$")


def source_priority_factor(path: str) -> float:
    """Multiplicative prior: documentation and tests are demoted, source is 1.0."""
    p = path.replace("\\", "/").lower()
    name = p.rsplit("/", 1)[-1]
    stem = name.split(".", 1)[0]

    # Legal files carry the heaviest demotion.
    if stem in {"license", "notice", "copying"}:
        return 0.1
    # The repository's main README keeps a neutral prior; a README in a
    # subdirectory is ordinary documentation, translated READMEs are demoted.
    if name in _PRIMARY_README_NAMES:
        return 1.0 if "/" not in p else 0.5
    if stem.startswith("readme"):
        return 0.2
    # Documentation directories, prose extensions and change records
    # describe the code without being it.
    if (
        any(f"/{part}/" in f"/{p}" for part in _DOCUMENT_DIRECTORIES)
        or p.endswith((".md", ".rst", ".txt"))
        or ("/" not in p and "." not in name and stem in _DOCUMENT_STEMS)
    ):
        return 0.5
    if is_test_path(p):
        return 0.6
    # Vendored third-party code is real source the project does not own; a
    # request about the project is looking for the code that calls into it.
    if any(f"/{part}/" in f"/{p}" for part in _VENDORED_DIRECTORIES):
        return 0.6
    # Configuration files and type stubs: a request that needs them names
    # the file (a path request, neutral prior); every other request is
    # looking for an implementation and these merely repeat its names.
    if (
        name.endswith(_CONFIG_SUFFIXES)
        or _RC_FILE.match(name)
        or name.endswith(".pyi")
        or ".config." in name
    ):
        return 0.7
    if name in {"index.ts", "index.tsx", "index.js", "index.jsx", "types.ts"}:
        return 0.85
    if name == "__init__.py":
        return 0.85
    # Editor integrations, shell glue and other files in a language the index
    # does not parse (an Emacs mode next to a Python linter) are real code but
    # rarely what a request about the project is looking for.
    if "." in name and detect_language(name) is None:
        return 0.85
    return 1.0


def is_root_readme(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    name = normalized.rsplit("/", 1)[-1]
    return "/" not in normalized and name.split(".", 1)[0] == "readme"


def neutral_priority_factor(_path: str) -> float:
    """Constant 1.0: the prior is off.

    Used when source priority is disabled and for "where is file X" path
    requests: any file kind may be the answer there (documentation,
    configuration, tests, a license all fall inside file semantics), so the
    source-first assumption does not hold and the order is left to the path
    boost, the content score and the reranker.
    """
    return 1.0
