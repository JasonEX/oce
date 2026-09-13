"""Build the semantic document the path index embeds for one file.

Only the path's own structure is used (directories, file-name pieces,
extension tokens) plus the extension-to-kind semantics that hold for any
repository. No specific file names or single-stack priors are injected, so
the path index does not degrade into a memory of one benchmark repository.
"""

from __future__ import annotations

import re

from oce.domain.services.source_filter import is_ignored_source_path

# Directory separators, dots, underscores, hyphens and camelCase boundaries
# split a path into matchable tokens.
_TOKEN_SPLIT = re.compile(r"[/\\._\-]+")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

# Extension to kind semantics. What an extension says about a file's language
# or kind is the same in every repository, so it is general knowledge rather
# than a single-repository prior; file and directory names are covered by the
# tokenizer below. The Chinese words let Chinese requests match too.
EXTENSION_SEMANTICS = {
    ".rs": "Rust source code 源码",
    ".py": "Python source code 源码",
    ".ts": "TypeScript source code 源码",
    ".tsx": "TypeScript React JSX component 组件 源码",
    ".js": "JavaScript source code 源码",
    ".jsx": "JavaScript React JSX component 组件 源码",
    ".vue": "Vue component 组件 源码",
    ".go": "Go source code 源码",
    ".java": "Java source code 源码",
    ".kt": "Kotlin source code 源码",
    ".rb": "Ruby source code 源码",
    ".php": "PHP source code 源码",
    ".cs": "C# source code 源码",
    ".cpp": "C++ source code 源码",
    ".c": "C source code 源码",
    ".h": "C/C++ header 头文件",
    ".json": "JSON configuration data 配置 数据",
    ".toml": "TOML configuration 配置",
    ".yaml": "YAML configuration 配置",
    ".yml": "YAML configuration 配置",
    ".ini": "INI configuration 配置",
    ".md": "Markdown documentation 文档",
    ".rst": "reStructuredText documentation 文档",
    ".sql": "SQL database schema query 数据库 查询",
}


def _tokenize(path: str) -> list[str]:
    """Lower-case path tokens (directories, name pieces, camelCase parts), deduplicated in order."""
    tokens: list[str] = []
    for part in _TOKEN_SPLIT.split(path):
        if not part:
            continue
        for piece in _CAMEL_BOUNDARY.split(part):
            piece = piece.strip().lower()
            if piece and piece not in tokens:
                tokens.append(piece)
    return tokens


def build_path_document(path: str) -> str:
    """The embedding text: full path, file name, stem, tokens and extension semantics."""
    normalized = path.replace("\\", "/")
    filename = normalized.rsplit("/", 1)[-1]
    stem = filename.split(".", 1)[0]

    parts: list[str] = [normalized, filename]
    if stem and stem != filename:
        parts.append(stem)
    parts.extend(_tokenize(normalized))

    for ext, keywords in EXTENSION_SEMANTICS.items():
        if filename.endswith(ext):
            parts.append(keywords)
            break

    seen: set[str] = set()
    ordered: list[str] = []
    for part in parts:
        if part and part not in seen:
            seen.add(part)
            ordered.append(part)
    return " ".join(ordered)


def is_indexable_path(path: str) -> bool:
    """The path index shares the source admission rules: no dependency, build, binary or secret paths."""
    return not is_ignored_source_path(path)
