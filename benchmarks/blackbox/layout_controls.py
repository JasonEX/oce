"""Synthetic rename, file-layout and line-offset controls through released oce-client.

These are four development needs across two languages, not independent real-project questions.
The fixture generator changes names, module filenames and leading blank lines
while preserving behavior, updating imports and truth locations together.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean

from benchmarks.blackbox.harness import (
    DEFAULT_WORKDIR,
    ensure_comparable,
    parse_retrieved_regions,
    percentile,
    resolve_client_binary,
    run_client,
    runtime_metadata,
)


@dataclass(frozen=True)
class Control:
    id: str
    language: str
    filename: str
    name: str
    offset: int
    files: dict[str, str]
    targets: dict[str, tuple[str, int]]


def controls() -> tuple[Control, ...]:
    fixtures = []
    for language, names, filenames in (
        ("python", ("parse_config", "collect_options"), ("parser.py", "__init__.py")),
        (
            "typescript",
            ("parseConfig", "collectOptions"),
            ("parser.ts", "index.ts", "types.ts"),
        ),
    ):
        for filename in filenames:
            for name in names:
                for offset in (0, 80):
                    folder = "pkg" if language == "python" else "src"
                    target = f"{folder}/{filename}"
                    if language == "python":
                        module = "pkg" if filename == "__init__.py" else "pkg.parser"
                        body = f'''def {name}(text):
    """Read key=value settings, ignoring blank lines and comments."""
    values = {{}}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values
'''
                        use_path = "pkg/consumer.py"
                        consumer = f"from {module} import {name}\n\ndef load_preferences(text):\n    return {name}(text)\n"
                        test_path = "tests/test_reader.py"
                        test = f'from {module} import {name}\n\ndef test_settings_reader():\n    assert {name}("# comment\\n\\nmode = fast\\n") == {{"mode": "fast"}}\n'
                        distractors = {
                            "pkg/writer.py": 'def write_settings(values):\n    """Write settings as key=value text."""\n    return "\\n".join(f"{k}={v}" for k, v in values.items())\n',
                            "pkg/validator.py": 'def validate_settings(values):\n    """Validate parsed settings keys and values."""\n    return all(key and value for key, value in values.items())\n',
                            "pkg/display.py": 'def describe_settings(values):\n    """Show configuration settings for display."""\n    return sorted(values.items())\n',
                        }
                        use_line, test_line = 4, 4
                    else:
                        module = filename.removesuffix(".ts")
                        body = f"""export function {name}(text: string): Record<string, string> {{
  // Read key=value settings, ignoring blank lines and comments.
  const values: Record<string, string> = {{}};
  for (const raw of text.split("\\n")) {{
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const separator = line.indexOf("=");
    values[line.slice(0, separator).trim()] = line.slice(separator + 1).trim();
  }}
  return values;
}}
"""
                        use_path = "src/consumer.ts"
                        consumer = f'import {{ {name} }} from "./{module}";\nexport function loadPreferences(text: string) {{\n  return {name}(text);\n}}\n'
                        test_path = "tests/reader.test.ts"
                        test = f'import {{ {name} }} from "../src/{module}";\nimport {{ strict as assert }} from "node:assert";\nexport function testSettingsReader() {{\n  assert.deepEqual({name}("# comment\\n\\nmode = fast\\n"), {{mode: "fast"}});\n}}\n'
                        distractors = {
                            "src/writer.ts": 'export function writeSettings(values: Record<string, string>): string {\n  // Write settings as key=value text.\n  return Object.entries(values).map(([k,v]) => `${k}=${v}`).join("\\n");\n}\n',
                            "src/validator.ts": "export function validateSettings(values: Record<string, string>): boolean {\n  // Validate parsed settings keys and values.\n  return Object.entries(values).every(([k,v]) => !!k && !!v);\n}\n",
                            "src/display.ts": "export function describeSettings(values: Record<string, string>) {\n  // Show configuration settings for display.\n  return Object.entries(values).sort();\n}\n",
                        }
                        use_line, test_line = 3, 4
                    files = {
                        target: "\n" * offset + body,
                        use_path: consumer,
                        test_path: test,
                        **distractors,
                    }
                    fixtures.append(
                        Control(
                            f"{language}-{filename}-{name}-{offset}",
                            language,
                            filename,
                            name,
                            offset,
                            files,
                            {
                                "definition": (target, offset + 1),
                                "semantic": (target, offset + 1),
                                "reference": (use_path, use_line),
                                "tests": (test_path, test_line),
                            },
                        )
                    )
    return tuple(fixtures)


def queries(control: Control) -> dict[str, str]:
    return {
        "definition": f"Where is {control.name} defined?",
        "semantic": "Read key=value settings from text, ignoring blank lines and comment lines.",
        "reference": f"callers of {control.name}",
        "tests": f"Locate tests exercising {control.name}.",
    }


def summarize(rows: Sequence[dict]) -> dict:
    ok = [row for row in rows if row["status"] == "ok"]
    groups = sorted({row["family_id"] for row in rows})
    return {
        "cases": len(rows),
        "successful_cases": len(ok),
        "error_cases": len(rows) - len(ok),
        "needs": len(groups),
        **{
            key: fmean(row.get(key, 0) for row in rows)
            for key in ("top1", "hit_at_3", "mrr")
        },
        "worst_family_hit_at_3": fmean(
            min(row.get("hit_at_3", 0) for row in rows if row["family_id"] == group)
            for group in groups
        ),
        "p50_elapsed_ms": percentile([row["elapsed_ms"] for row in ok], 50),
        "mean_returned_chars": fmean(row["returned_chars"] for row in ok)
        if ok
        else None,
    }


def run(args: argparse.Namespace) -> dict:
    fixtures = controls()
    spec = json.dumps(
        [{**asdict(item), "queries": queries(item)} for item in fixtures],
        sort_keys=True,
    ).encode()
    binary = resolve_client_binary(args.client_binary)
    api_key = os.environ.get("OCE_API_KEY", "sk-opencontextengine")
    workdir = args.workdir.expanduser().resolve() / "layout-controls"
    results = []
    for control in fixtures:
        root = workdir / "sources" / control.id
        state = workdir / "client-state" / f"{control.id}.sqlite3"
        state.parent.mkdir(parents=True, exist_ok=True)
        for relative, content in control.files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.read_text() != content:
                raise ValueError("layout fixture changed; use a fresh workdir")
            path.write_text(content)
        observed = {
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file()
        }
        if observed != set(control.files):
            raise ValueError("unexpected file in layout fixture")
        print(f"[sync] {control.id}", file=sys.stderr)
        run_client(
            binary, root, state, args.api_url, api_key, ("sync", "--json"), retries=4
        )
        for kind, query in queries(control).items():
            row = {
                "id": f"{control.id}-{kind}",
                "family_id": kind,
                "kind": kind,
                "language": control.language,
                "filename": control.filename,
                "name": control.name,
                "offset": control.offset,
                "query": query,
                "target": control.targets[kind],
            }
            try:
                response = run_client(
                    binary,
                    root,
                    state,
                    args.api_url,
                    api_key,
                    ("retrieve", query, "--json"),
                )
                formatted = response["formatted_retrieval"]
                regions = parse_retrieved_regions(formatted)
                path, line = control.targets[kind]
                rank = next(
                    (
                        i
                        for i, hit in enumerate(regions, 1)
                        if hit.path == path and hit.start <= line <= hit.end
                    ),
                    None,
                )
                row.update(
                    status="ok",
                    top1=float(rank == 1),
                    hit_at_3=float(rank is not None and rank <= 3),
                    mrr=1 / rank if rank else 0,
                    elapsed_ms=response["elapsed_ms"],
                    returned_chars=len(formatted),
                    retrieved=[asdict(hit) for hit in regions],
                )
            except Exception as exc:
                row.update(
                    status="error",
                    error_type=type(exc).__name__,
                    error=str(exc).replace(api_key, "[REDACTED]")[:1000],
                )
            results.append(row)
    return {
        "schema_version": 1,
        "suite": "layout_controls",
        "label": args.label,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_revisions": {"fixture_spec_sha256": hashlib.sha256(spec).hexdigest()},
        "runtime": runtime_metadata(
            binary=binary,
            api_url=args.api_url,
            admin_key=os.environ.get("OCE_ADMIN_API_KEY"),
            extra_metadata=args.metadata,
        ),
        "case_ids": [row["id"] for row in results],
        "summary": summarize(results),
        "by_family": {
            family: summarize([row for row in results if row["family_id"] == family])
            for family in sorted({row["family_id"] for row in results})
        },
        "cases": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    runner = commands.add_parser("run")
    runner.add_argument("--api-url", required=True)
    runner.add_argument("--workdir", type=Path, default=DEFAULT_WORKDIR)
    runner.add_argument("--client-binary", type=Path)
    runner.add_argument("--label", required=True)
    runner.add_argument("--metadata", action="append", default=[])
    runner.add_argument("--output", type=Path, required=True)
    comparison = commands.add_parser("compare")
    comparison.add_argument("results", nargs="+", type=Path)
    args = parser.parse_args()
    if args.command == "run":
        report = run(args)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report["summary"], indent=2))
    else:
        reports = [json.loads(path.read_text()) for path in args.results]
        ensure_comparable(reports, suite="layout_controls")
        print(
            "| Variant | Needs | Top-1 | Hit@3 | Worst family Hit@3 | MRR | Chars | p50 ms |"
        )
        print("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for report in reports:
            s = report["summary"]
            print(
                f"| {report['label']} | {s['needs']} | {s['top1']:.3f} | {s['hit_at_3']:.3f} | {s['worst_family_hit_at_3']:.3f} | {s['mrr']:.3f} | {s['mean_returned_chars']:.0f} | {s['p50_elapsed_ms']} |"
            )


if __name__ == "__main__":
    main()
