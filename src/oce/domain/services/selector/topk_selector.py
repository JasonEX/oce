"""Top-k result selector."""

from __future__ import annotations

from oce.domain.services.search import SearchHit
from oce.domain.services.selector.protocols import SelectionMode


class TopKSelector:
    async def select(
        self,
        hits: list[SearchHit],
        top_k: int,
        *,
        mode: SelectionMode = SelectionMode.COVERAGE,
        max_chars: int | None = None,
    ) -> list[SearchHit]:
        return hits[:top_k]
