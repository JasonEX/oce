"""Source-file admission rules for indexing."""

from __future__ import annotations

IGNORED_DIRECTORY_NAMES = frozenset(
    {
        ".cache",
        ".eggs",
        ".git",
        ".gradle",
        ".hg",
        ".idea",
        ".mypy_cache",
        ".next",
        ".nuxt",
        ".output",
        ".pytest_cache",
        ".ruff_cache",
        ".svelte-kit",
        ".svn",
        ".tox",
        ".turbo",
        ".venv",
        ".vscode",
        "bower_components",
        "build",
        "coverage",
        "dist",
        "generated",
        "node_modules",
        "site-packages",
        "target",
        "vendor",
        "venv",
        "__pycache__",
    }
)

IGNORED_FILE_SUFFIXES = (
    ".min.js",
    ".min.css",
    ".map",
    ".lock",
    ".log",
    ".tmp",
    ".bak",
    ".swp",
    ".jsonl",
    ".csv",
    ".tsv",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".ico",
    ".icns",
    ".webp",
    ".tiff",
    ".svg",
    ".mp3",
    ".mp4",
    ".wav",
    ".avi",
    ".mov",
    ".flac",
    ".ogg",
    ".webm",
    ".mkv",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".eot",
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".zip",
    ".tar",
    ".gz",
    ".tgz",
    ".rar",
    ".7z",
    ".bz2",
    ".xz",
    ".pyc",
    ".pyo",
    ".class",
    ".o",
    ".obj",
    ".a",
    ".so",
    ".dll",
    ".dylib",
    ".exe",
    ".bin",
    ".wasm",
    ".sqlite",
    ".db",
)

SENSITIVE_DIRECTORY_NAMES = frozenset({".aws", ".ssh"})
SENSITIVE_FILE_NAMES = frozenset(
    {
        ".git-credentials",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "id_ed25519",
        "id_rsa",
    }
)
SENSITIVE_FILE_SUFFIXES = (".pem", ".key", ".p12", ".pfx")
SAFE_ENV_SUFFIXES = (".example", ".sample", ".template")


def is_ignored_source_path(path: str) -> bool:
    """Return whether a path is dependency, generated, or non-source content."""
    normalized = path.replace("\\", "/").casefold()
    parts = tuple(part for part in normalized.split("/") if part)
    filename = parts[-1] if parts else ""
    if any(
        part in IGNORED_DIRECTORY_NAMES or part.endswith(".egg-info")
        for part in parts[:-1]
    ):
        return True
    if any(part in SENSITIVE_DIRECTORY_NAMES for part in parts[:-1]):
        return True
    if filename in SENSITIVE_FILE_NAMES:
        return True
    if filename == ".env" or (
        filename.startswith(".env.") and not filename.endswith(SAFE_ENV_SUFFIXES)
    ):
        return True
    if filename.endswith(SENSITIVE_FILE_SUFFIXES):
        return True
    return normalized.endswith(IGNORED_FILE_SUFFIXES)


def is_binary_source(content: str) -> bool:
    """Use the same NUL-byte signal as Git's inexpensive binary check."""
    return "\x00" in content
