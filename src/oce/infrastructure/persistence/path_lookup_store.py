"""Exact path and basename lookup inside a scope; the SQL twin of the path index."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from sqlalchemy import case, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from oce.domain.blob.blob import BlobStatus
from oce.domain.services.search import SearchScope
from oce.infrastructure.persistence.models import BlobModel
from oce.infrastructure.persistence.scope_filter import run_scoped

_FULL_SUFFIX_SCORE = 1.0
_BASENAME_SCORE = 0.9
_MAX_PATTERNS = 24


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


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
        """``blob_name -> score``: a two-segment path suffix beats a bare basename.

        Traceback paths are often absolute or from another checkout, so only
        their last two segments are matched; that is enough to separate
        ``core/dataset.py`` from ``backends/dataset.py``.
        """
        if not scope.blob_names:
            return {}
        suffixes: list[str] = []
        for raw in paths:
            segments = raw.replace("\\", "/").strip("/").split("/")
            if len(segments) >= 2:
                suffix = "/".join(segments[-2:])
                if suffix not in suffixes:
                    suffixes.append(suffix)
        basenames = [name for name in dict.fromkeys(filenames)]
        suffixes = suffixes[:_MAX_PATTERNS]
        basenames = basenames[:_MAX_PATTERNS]
        if not suffixes and not basenames:
            return {}

        def build(predicate: ColumnElement[bool]):
            suffix_conditions = [
                BlobModel.path.like(f"%/{_escape_like(suffix)}", escape="\\")
                for suffix in suffixes
            ]
            suffix_conditions.extend(BlobModel.path == suffix for suffix in suffixes)
            basename_conditions = [
                BlobModel.path.like(f"%/{_escape_like(name)}", escape="\\")
                for name in basenames
            ]
            basename_conditions.extend(BlobModel.path == name for name in basenames)
            conditions = [*suffix_conditions, *basename_conditions]
            score = (
                case(
                    (or_(*suffix_conditions), _FULL_SUFFIX_SCORE),
                    else_=_BASENAME_SCORE,
                )
                if suffix_conditions
                else literal(_BASENAME_SCORE)
            ).label("match_score")
            return (
                select(BlobModel.blob_name, BlobModel.path, score)
                .where(
                    BlobModel.status == BlobStatus.READY.value,
                    or_(*conditions),
                    predicate,
                )
                .order_by(score.desc(), BlobModel.path, BlobModel.blob_name)
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
