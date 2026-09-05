"""Offline view of routing decisions against the structural evidence they saw.

Reads ``retrieval_metrics`` from a server database and tabulates the rerank
route by intent, definition count and ambiguity, alongside the size of the
relation sections. This is the shadow log the adaptive thresholds are
calibrated from: join it with per-case benchmark results (by query text when
``MONITORING_STORE_QUERY_TEXT`` was on) to see which evidence patterns
benefited from a reranker and which were already answered. It is an internal
diagnostic and reads server persistence directly, so it cannot support a
product-utility claim on its own.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Sequence
from pathlib import Path

COLUMNS = (
    "intent",
    "rerank_route",
    "head_slots",
    "exact_definitions",
    "definition_sites",
    "relation_hits",
    "relation_chars",
    "hit_count",
    "total_ms",
    "query_text",
)


def load_rows(
    database: Path, *, since_hours: float | None, source: str = "retrieval"
) -> list[dict[str, object]]:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        sql = f"SELECT {', '.join(COLUMNS)} FROM retrieval_metrics WHERE source = ?"
        params: tuple[object, ...] = (source,)
        if since_hours is not None:
            sql += " AND ts >= datetime('now', ?)"
            params = (f"-{since_hours} hours",)
        return [dict(row) for row in connection.execute(sql, params)]
    finally:
        connection.close()


def _ambiguity(row: dict[str, object]) -> str:
    sites = int(row.get("definition_sites") or 0)
    slots = int(row.get("head_slots") or 0)
    if sites == 0:
        return "no_definition"
    if sites <= max(slots, 1):
        return "fits_head"
    return "overflows_head"


def summarize(rows: Sequence[dict[str, object]]) -> dict[str, object]:
    by_key: dict[tuple[str, str, str], list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        by_key[(str(row["intent"]), _ambiguity(row), str(row["rerank_route"]))].append(
            row
        )
    table = []
    for (intent, ambiguity, route), items in sorted(by_key.items()):
        relation = [int(item.get("relation_hits") or 0) for item in items]
        chars = [int(item.get("relation_chars") or 0) for item in items]
        latency = sorted(int(item.get("total_ms") or 0) for item in items)
        table.append(
            {
                "intent": intent,
                "ambiguity": ambiguity,
                "route": route,
                "queries": len(items),
                "mean_relation_hits": sum(relation) / len(items),
                "mean_relation_chars": sum(chars) / len(items),
                "p50_total_ms": latency[len(latency) // 2],
                "with_relations": sum(1 for value in relation if value > 0),
            }
        )
    routes = Counter(str(row["rerank_route"]) for row in rows)
    return {"rows": len(rows), "routes": dict(routes), "table": table}


def render(summary: dict[str, object]) -> str:
    headers = (
        "Intent",
        "Ambiguity",
        "Route",
        "Queries",
        "With relations",
        "Mean relation hits",
        "Mean relation chars",
        "p50 ms",
    )
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for item in summary["table"]:  # type: ignore[index]
        lines.append(
            "| "
            + " | ".join(
                (
                    str(item["intent"]),
                    str(item["ambiguity"]),
                    str(item["route"]),
                    str(item["queries"]),
                    str(item["with_relations"]),
                    f"{float(item['mean_relation_hits']):.2f}",
                    f"{float(item['mean_relation_chars']):.0f}",
                    str(item["p50_total_ms"]),
                )
            )
            + " |"
        )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "database", type=Path, help="path to the server's SQLite oce.db"
    )
    parser.add_argument("--since-hours", type=float)
    parser.add_argument(
        "--source",
        default="retrieval",
        help="metrics source to read (product retrieval rows use 'retrieval')",
    )
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    rows = load_rows(
        args.database.expanduser().resolve(),
        since_hours=args.since_hours,
        source=args.source,
    )
    summary = summarize(rows)
    print(json.dumps(summary, indent=2) if args.json else render(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
