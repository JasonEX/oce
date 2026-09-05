"""Pinned CodeSearchNet functions as an external sanity guard for retrieval.

CodeSearchNet pairs a function with its docstring and a GitHub permalink that
carries the repository commit, so every case can be replayed against a clean
snapshot without redistributing the corpus. The public benchmark itself is
archived; this module only reuses its data as a natural-language-to-function
retrieval check across four languages. CrossCodeEval and RepoBench-R were
considered first, but their raw repositories are not public or not pinned to
commits, which makes them unusable behind the black-box boundary.

Case selection is deterministic: repositories with a moderate number of test
functions are ordered by a salted hash, the first reachable ones per language
are kept, and functions with a substantive first docstring paragraph are
sampled by the same hash. The resulting manifest is checked in; the parquet
files are needed only to regenerate it.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import cast

from benchmarks.blackbox.corpus import CodeLanguage, RepositorySnapshot
from benchmarks.blackbox.harness import sha256_file

DATASET_REVISION = "bd0cf261e357a3eb5c8fba490d23ec1a1cd59555"
PARQUET_FILES: dict[CodeLanguage, tuple[str, str]] = {
    "python": (
        "https://huggingface.co/api/datasets/code-search-net/code_search_net/parquet/python/test/0.parquet",
        "3167e79ee7f081d825bf97b96d3a6b2d96428b00f6a98125be943384d8afae5f",
    ),
    "go": (
        "https://huggingface.co/api/datasets/code-search-net/code_search_net/parquet/go/test/0.parquet",
        "cb78d8951e9623bc2460d9b29c3c4152724f28c4f685fc019e0aafa071d2b18f",
    ),
    "java": (
        "https://huggingface.co/api/datasets/code-search-net/code_search_net/parquet/java/test/0.parquet",
        "1f39dc48cf7e5d99cfe41c997eca8f2e7291cb570ec4d26e74a76a188eb8fa1c",
    ),
    "javascript": (
        "https://huggingface.co/api/datasets/code-search-net/code_search_net/parquet/javascript/test/0.parquet",
        "8ef888d473f7e9cd6e38c116cd50749743de8a7528e3bcb29f3c45d2f3992580",
    ),
}
SELECTION_SALT = "oce-csn-v1"
MIN_REPO_FUNCTIONS = 40
MAX_REPO_FUNCTIONS = 250
MIN_QUERY_WORDS = 6
MAX_QUERY_CHARS = 400
_URL_RE = re.compile(r"/blob/([0-9a-f]{40})/(.+)#L(\d+)-L(\d+)$")
_TAG_LINE_RE = re.compile(
    r"^\s*(?:@\w+|:\w+|Args:|Returns?:|Raises?:|Parameters|Yields?:|Example|Note)"
)


@dataclass(frozen=True)
class FunctionCase:
    id: str
    instance_id: str
    query: str
    path: str
    start: int
    end: int
    func_name: str
    name_in_query: bool


def _download(url: str, destination: Path, expected_sha256: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and sha256_file(destination) == expected_sha256:
        return
    partial = destination.with_suffix(destination.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=300) as response:  # noqa: S310
            with partial.open("wb") as output:
                shutil.copyfileobj(response, output)
        actual = sha256_file(partial)
        if actual != expected_sha256:
            raise RuntimeError(
                f"checksum mismatch for {destination.name}: "
                f"expected {expected_sha256}, got {actual}"
            )
        partial.replace(destination)
    finally:
        partial.unlink(missing_ok=True)


def prepare_parquet(workdir: Path, language: CodeLanguage) -> Path:
    url, digest = PARQUET_FILES[language]
    path = workdir / "datasets" / f"csn-{language}-test.parquet"
    _download(url, path, digest)
    return path


def query_from_docstring(docstring: str) -> str | None:
    """First paragraph of a docstring, without parameter tags or code fences."""
    lines: list[str] = []
    for raw in docstring.replace("\r", "").split("\n"):
        line = raw.strip().lstrip("/*# ").rstrip("*/ ")
        if not line:
            if lines:
                break
            continue
        if _TAG_LINE_RE.match(line) or line.startswith("```"):
            break
        lines.append(line)
    text = " ".join(lines).strip()
    text = re.sub(r"\s+", " ", text)
    if len(text.split()) < MIN_QUERY_WORDS or len(text) > MAX_QUERY_CHARS:
        return None
    return text


def _hash(value: str) -> bytes:
    return hashlib.sha256(f"{SELECTION_SALT}:{value}".encode()).digest()


def _name_in_query(func_name: str, query: str) -> bool:
    leaf = func_name.rsplit(".", 1)[-1]
    parts = [part for part in re.split(r"[_\W]+|(?<=[a-z])(?=[A-Z])", leaf) if part]
    lowered = query.lower()
    return leaf.lower() in lowered or (
        len(parts) > 1 and all(part.lower() in lowered for part in parts)
    )


def _load_rows(path: Path) -> list[dict[str, object]]:
    try:
        import pyarrow.parquet as parquet
    except ImportError as exc:  # pragma: no cover - installation guidance
        raise RuntimeError("pyarrow is required; run `uv sync --extra dev`") from exc
    columns = [
        "repository_name",
        "func_path_in_repository",
        "func_name",
        "func_documentation_string",
        "func_code_url",
    ]
    return cast(
        list[dict[str, object]], parquet.read_table(path, columns=columns).to_pylist()
    )


def select_cases(
    workdir: Path,
    *,
    repositories_per_language: int,
    cases_per_repository: int,
    reachable: Callable[[str], bool],
) -> tuple[list[RepositorySnapshot], list[FunctionCase]]:
    snapshots: list[RepositorySnapshot] = []
    cases: list[FunctionCase] = []
    for language in PARQUET_FILES:
        rows = _load_rows(prepare_parquet(workdir, language))
        by_repo: dict[str, list[dict[str, object]]] = {}
        for row in rows:
            by_repo.setdefault(str(row["repository_name"]), []).append(row)
        candidates = sorted(
            (
                repo
                for repo, items in by_repo.items()
                if MIN_REPO_FUNCTIONS <= len(items) <= MAX_REPO_FUNCTIONS
            ),
            key=_hash,
        )
        picked = 0
        for repo in candidates:
            if picked >= repositories_per_language:
                break
            parsed = [
                (row, _URL_RE.search(str(row["func_code_url"])))
                for row in by_repo[repo]
            ]
            commits = {match.group(1) for _, match in parsed if match}
            if len(commits) != 1 or not reachable(repo):
                continue
            revision = commits.pop()
            snapshot_id = f"csn-{repo.replace('/', '__')}"
            functions: list[FunctionCase] = []
            for row, match in sorted(
                parsed, key=lambda item: _hash(str(item[0]["func_code_url"]))
            ):
                if match is None:
                    continue
                query = query_from_docstring(str(row["func_documentation_string"]))
                path = match.group(2)
                if query is None or "/test" in f"/{path.lower()}":
                    continue
                func_name = str(row["func_name"])
                functions.append(
                    FunctionCase(
                        id=f"{snapshot_id}-{len(functions) + 1:02d}",
                        instance_id=snapshot_id,
                        query=query,
                        path=path,
                        start=int(match.group(3)),
                        end=int(match.group(4)),
                        func_name=func_name,
                        name_in_query=_name_in_query(func_name, query),
                    )
                )
                if len(functions) >= cases_per_repository:
                    break
            if len(functions) < cases_per_repository:
                continue
            snapshots.append(
                RepositorySnapshot(
                    id=snapshot_id, repo=repo, revision=revision, code_language=language
                )
            )
            cases.extend(functions)
            picked += 1
    return snapshots, cases


def write_manifest(
    path: Path, snapshots: Sequence[RepositorySnapshot], cases: Sequence[FunctionCase]
) -> None:
    payload = {
        "schema_version": 1,
        "source": {
            "dataset": "code-search-net/code_search_net",
            "dataset_revision": DATASET_REVISION,
            "split": "test",
            "selection_salt": SELECTION_SALT,
        },
        "snapshots": [asdict(snapshot) for snapshot in snapshots],
        "cases": [asdict(case) for case in cases],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", "utf-8")
