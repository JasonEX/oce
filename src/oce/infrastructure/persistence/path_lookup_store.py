"""Exact path and basename lookup inside a scope; the SQL twin of the path index."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from sqlalchemy import Select, case, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from oce.domain.blob.blob import BlobStatus
from oce.domain.services.search import SearchScope
from oce.infrastructure.persistence.models import BlobModel
from oce.infrastructure.persistence.scope_filter import run_scoped

_FULL_SUFFIX_SCORE = 1.0
_PARTIAL_SUFFIX_SCORE = 0.95
_BASENAME_SCORE = 0.9
_MAX_PATTERNS = 24
_MAX_FULL_SUFFIXES = 96


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _requested_tails(paths: Sequence[tuple[str, ...]]) -> list[str]:
    """Bound candidate paths while retaining likely repository-relative tails."""
    tails: list[str] = []
    max_depth = max((len(path) for path in paths), default=0)
    # Shorter tails are more likely to be repository-relative when a traceback
    # contains an arbitrary checkout prefix. Interleave paths by depth so one
    # unusually deep path cannot consume the entire SQL parameter budget.
    for depth in range(2, max_depth + 1):
        for path in paths:
            if len(path) < depth:
                continue
            tail = "/".join(path[-depth:])
            if tail not in tails:
                tails.append(tail)
            if len(tails) >= _MAX_FULL_SUFFIXES:
                return tails
    return tails


class SqlPathLookupStore:
    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def match_paths(
        self,
        *,
        filenames: Sequence[str],
        paths: Sequence[str],
        scope: SearchScope,
        limit: int = 20,
    ) -> dict[str, float]:
        """``blob_name -> score``: whole path > two-segment suffix > basename.

        Traceback paths are often absolute or from another checkout, so the
        SQL filter matches their last two segments; that is enough to separate
        ``core/dataset.py`` from ``backends/dataset.py``. Among the rows that
        pass, a file whose entire path is the tail of the request outranks one
        that only shares the suffix, so ``axum/src/routing/mod.rs`` beats
        ``axum-extra/src/routing/mod.rs`` when the request names the former.
        """
        if not scope.blob_names:
            return {}
        suffixes: list[str] = []
        requested: list[tuple[str, ...]] = []
        for raw in paths:
            segments = tuple(part for part in raw.replace("\\", "/").split("/") if part)
            if len(segments) >= 2:
                requested.append(segments)
                suffix = "/".join(segments[-2:])
                if suffix not in suffixes:
                    suffixes.append(suffix)
        basenames = [name for name in dict.fromkeys(filenames)]
        suffixes = suffixes[:_MAX_PATTERNS]
        basenames = basenames[:_MAX_PATTERNS]
        full_suffixes = _requested_tails(requested)
        if not suffixes and not basenames:
            return {}

        def build(predicate: ColumnElement[bool]) -> Select[Any]:
            full_suffix_conditions: list[ColumnElement[bool]] = (
                [BlobModel.path.in_(full_suffixes)] if full_suffixes else []
            )
            suffix_conditions: list[ColumnElement[bool]] = [
                BlobModel.path.like(f"%/{_escape_like(suffix)}", escape="\\")
                for suffix in suffixes
            ]
            suffix_conditions.extend(BlobModel.path == suffix for suffix in suffixes)
            basename_conditions: list[ColumnElement[bool]] = [
                BlobModel.path.like(f"%/{_escape_like(name)}", escape="\\")
                for name in basenames
            ]
            basename_conditions.extend(BlobModel.path == name for name in basenames)
            conditions = [
                *full_suffix_conditions,
                *suffix_conditions,
                *basename_conditions,
            ]
            score_cases = []
            if full_suffix_conditions:
                score_cases.append((or_(*full_suffix_conditions), _FULL_SUFFIX_SCORE))
            if suffix_conditions:
                score_cases.append((or_(*suffix_conditions), _PARTIAL_SUFFIX_SCORE))
            score = (
                case(*score_cases, else_=_BASENAME_SCORE)
                if score_cases
                else literal(_BASENAME_SCORE)
            ).label("match_score")
            return (
                select(BlobModel.blob_name, BlobModel.path, score)
                .where(
                    BlobModel.status == BlobStatus.READY.value,
                    or_(*conditions),
                    predicate,
                )
                .order_by(
                    score.desc(),
                    func.length(BlobModel.path).desc(),
                    BlobModel.path,
                    BlobModel.blob_name,
                )
                .limit(max(limit * 4, limit))
            )

        async with self._session_factory() as session:
            rows = await run_scoped(session, scope, BlobModel.blob_name, build)

        scores: dict[str, float] = {}
        for row in rows:
            score = float(row.match_score)
            if score > scores.get(row.blob_name, 0.0):
                scores[row.blob_name] = score
        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        return dict(ranked[:limit])
