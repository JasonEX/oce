"""Map a file path to the chunking language identifier.

``LanguageChunkerRouter`` dispatches the identifier to an AST or document
chunker; unknown files fall back to ``RecursiveChunker``.
"""

from __future__ import annotations

import os

# Lower-case extension (with the dot) to the normalized language identifier.
_EXT_TO_LANG: dict[str, str] = {
    # Python
    ".py": "python",
    ".pyi": "python",
    # Java
    ".java": "java",
    # Java Server Pages and tag files
    ".jsp": "jsp",
    ".jspx": "jsp",
    ".jspf": "jsp",
    ".tag": "jsp",
    ".tagx": "jsp",
    # C#
    ".cs": "csharp",
    # TypeScript / TSX
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    # JavaScript / JSX
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "jsx",
    # C / C++
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".cc": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
    # Go
    ".go": "go",
    # Rust
    ".rs": "rust",
    # Ruby
    ".rb": "ruby",
    # PHP
    ".php": "php",
    # Swift
    ".swift": "swift",
    # Kotlin
    ".kt": "kotlin",
    ".kts": "kotlin",
    # Scala
    ".scala": "scala",
    ".sc": "scala",
    # HTML / CSS
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    # Data formats
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".xml": "xml",
    # Markdown
    ".md": "markdown",
    ".markdown": "markdown",
    ".mdx": "markdown",
    # Shell
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    # SQL
    ".sql": "sql",
    # Lua
    ".lua": "lua",
    # R
    ".r": "r",
    ".R": "r",
    # Julia
    ".jl": "julia",
    # Haskell
    ".hs": "haskell",
    # Elixir
    ".ex": "elixir",
    ".exs": "elixir",
    # Erlang
    ".erl": "erlang",
    ".hrl": "erlang",
    # Clojure
    ".clj": "clojure",
    ".cljs": "clojure",
    ".cljc": "clojure",
    # OCaml
    ".ml": "ocaml",
    ".mli": "ocaml",
    # Zig
    ".zig": "zig",
    # Nim
    ".nim": "nim",
    # Dart
    ".dart": "dart",
    # Perl
    ".pl": "perl",
    ".pm": "perl",
    # Dockerfile
    "Dockerfile": "dockerfile",
    # Makefile
    "Makefile": "make",
    "makefile": "make",
    # CMake
    ".cmake": "cmake",
    # Vue / Svelte
    ".vue": "vue",
    ".svelte": "svelte",
}

# Every identifier the router accepts.
SUPPORTED_LANGUAGES: frozenset[str] = frozenset(_EXT_TO_LANG.values())


def detect_language(path: str) -> str | None:
    """The language identifier for ``path``, or None when unsupported.

    The extension decides first; files without one (Dockerfile, Makefile)
    match by base name.
    """
    _, ext = os.path.splitext(path.lower())
    if ext and ext in _EXT_TO_LANG:
        return _EXT_TO_LANG[ext]
    basename = os.path.basename(path)
    return _EXT_TO_LANG.get(basename)
