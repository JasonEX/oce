"""Bump the project version.

Usage:
    python scripts/bump_version.py <major|minor|patch|version> [--commit] [--dry-run]

``[project].version`` in pyproject.toml is the single source of truth and
``src/oce/__init__.py`` is kept in sync. Lines are replaced in place rather
than rewriting the TOML, so the comments in pyproject.toml survive.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
INIT_FILE = REPO_ROOT / "src/oce/__init__.py"

# Basic PEP 440: N(.N)* with optional pre, .postN and .devN parts.
_PEP440_RE = re.compile(r"^\d+(?:\.\d+)*(?:[ab]|rc)?\d*(?:\.post\d+)?(?:\.dev\d+)?$")

_PARTS = ("major", "minor", "patch")


def _stdout() -> None:
    """Force UTF-8 output under Windows PowerShell."""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:  # not a real stream
        pass


def read_current_version() -> str:
    """The version in the [project] table."""
    lines = PYPROJECT.read_text(encoding="utf-8").splitlines()
    in_project = False
    for line in lines:
        stripped = line.strip()
        if stripped == "[project]":
            in_project = True
            continue
        if in_project and stripped.startswith("["):
            break
        if in_project:
            m = re.fullmatch(r'version\s*=\s*"([^"]+)"', stripped)
            if m:
                return m.group(1)
    raise SystemExit(f"ERROR: no version field in the [project] table of {PYPROJECT}")


def resolve_version(arg: str, current: str) -> str:
    """The target version for a bump part or an explicit version."""
    if arg in _PARTS:
        # A pre-release suffix (0.1.0rc1) is dropped; bumps stay three-part.
        parts = [int(p) for p in current.split(".")[:3]]
        while len(parts) < 3:
            parts.append(0)
        if arg == "major":
            parts[0] += 1
            parts[1] = 0
            parts[2] = 0
        elif arg == "minor":
            parts[1] += 1
            parts[2] = 0
        else:
            parts[2] += 1
        return ".".join(str(p) for p in parts)
    if not _PEP440_RE.fullmatch(arg):
        raise SystemExit(f"ERROR: '{arg}' is not a valid PEP 440 version")
    return arg


def _replace_in_file(path: Path, pattern: re.Pattern, new: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    hit = False
    for i, line in enumerate(lines):
        if pattern.search(line):
            lines[i] = pattern.sub(new, line)
            hit = True
    if not hit:
        raise SystemExit(f"ERROR: no line to replace in {path}")
    path.write_text("".join(lines), encoding="utf-8")


def update_pyproject(version: str) -> None:
    """Replace the version line; anchored at line start so minversion is untouched."""
    _replace_in_file(
        PYPROJECT, re.compile(r'^version\s*=\s*"[^"]*"'), f'version = "{version}"'
    )


def update_init(version: str) -> None:
    _replace_in_file(
        INIT_FILE,
        re.compile(r'^__version__\s*=\s*"[^"]*"'),
        f'__version__ = "{version}"',
    )


def verify_sync() -> None:
    """Check that both version locations agree."""
    init_text = INIT_FILE.read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', init_text)
    init_version = m.group(1) if m else None
    if init_version != read_current_version():
        raise SystemExit(
            f"ERROR: version mismatch pyproject={read_current_version()} __init__={init_version!r}"
        )


def git_commit(version: str) -> None:
    subprocess.run(
        ["git", "add", "pyproject.toml", "src/oce/__init__.py"],
        cwd=REPO_ROOT,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", f"chore(release): v{version}"],
        cwd=REPO_ROOT,
        check=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bump the version in pyproject.toml and src/oce/__init__.py"
    )
    parser.add_argument(
        "version_or_part", help="major, minor, patch, or an explicit version"
    )
    parser.add_argument(
        "--commit", action="store_true", help="git commit after updating"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print the change without applying it"
    )
    args = parser.parse_args(argv)

    current = read_current_version()
    target = resolve_version(args.version_or_part, current)
    if current == target:
        print(f"already at {current}; nothing to do")
        return 0

    if args.dry_run:
        print(f"[dry-run] {current} -> {target}")
        return 0

    update_pyproject(target)
    update_init(target)
    verify_sync()
    if args.commit:
        git_commit(target)
    print(f"version updated: {current} -> {target}")
    return 0


if __name__ == "__main__":
    _stdout()
    raise SystemExit(main())
