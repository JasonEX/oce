"""Release orchestration: bump, changelog, build, commit, tag.

Usage:
    python scripts/release.py <major|minor|patch|version> [--dry-run]

The working tree must be clean. The version is bumped, the changelog
section generated and prepended, dist/ built, and one commit plus an
annotated tag created. ``--dry-run`` prints the plan and changes nothing.
Pushing the tag makes GitHub Actions publish the GHCR image; the package
visibility must be confirmed after the first release.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bump_version
import generate_changelog

REPO_ROOT = Path(__file__).resolve().parent.parent

BUNDLED_FILES = ["pyproject.toml", "src/oce/__init__.py", "uv.lock", "CHANGELOG.md"]


def _stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass


def ensure_clean() -> None:
    out = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    dirty = [line for line in out.stdout.splitlines() if line.strip()]
    if dirty:
        details = "\n".join(dirty)
        raise SystemExit(
            f"ERROR: uncommitted changes; commit or stash first:\n{details}"
        )


def run(cmd: list[str]) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, cwd=REPO_ROOT, check=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Release a new version (bump, changelog, build, tag)"
    )
    parser.add_argument(
        "version_or_part", help="major, minor, patch, or an explicit version"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan without changing anything",
    )
    args = parser.parse_args(argv)

    ensure_clean()
    current = bump_version.read_current_version()
    target = bump_version.resolve_version(args.version_or_part, current)
    if current == target:
        raise SystemExit(f"already at {current}; nothing to release")

    print(f"==> releasing {target} (current {current})")
    print("plan:")
    print(f"  1. bump pyproject.toml + __init__.py -> {target}")
    print("  2. uv lock")
    print("  3. generate and prepend the CHANGELOG section")
    print("  4. uv build")
    print(f"  5. git commit 'chore(release): v{target}' + git tag v{target}")
    if args.dry_run:
        print("[dry-run] done; nothing changed")
        return 0

    bump_version.update_pyproject(target)
    bump_version.update_init(target)
    bump_version.verify_sync()
    print(f"version updated: {current} -> {target}")

    # The bump touches pyproject and __init__ only; uv.lock still records the
    # old root version and `uv sync --locked` in CI would fail, so the lock is
    # refreshed and committed with the release.
    run(["uv", "lock"])
    print("uv.lock refreshed")

    section = generate_changelog.build_section(
        target, since=generate_changelog.latest_tag()
    )
    if "### " not in section:
        raise SystemExit("ERROR: no releasable commits since the latest tag")
    generate_changelog.prepend(section)
    print(f"CHANGELOG updated: {generate_changelog.CHANGELOG}")

    run(["uv", "build"])
    run(["git", "add", *BUNDLED_FILES])
    run(["git", "commit", "-m", f"chore(release): v{target}"])
    run(
        [
            "git",
            "tag",
            "-a",
            f"v{target}",
            "-m",
            f"v{target} ({datetime.now(timezone.utc).date().isoformat()})",
        ]
    )

    print(f"\nreleased v{target}")
    print("next steps:")
    print("  git push && git push --tags")
    print("  wait for GitHub Actions to publish ghcr.io/jasonex/oce")
    print("  first public release: set the package visibility to Public")
    return 0


if __name__ == "__main__":
    _stdout()
    raise SystemExit(main())
