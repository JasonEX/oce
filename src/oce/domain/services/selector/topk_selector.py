"""Top-k result selector."""

from __future__ import annotations

from oce.domain.services.search import SearchHit
from oce.domain.services.selector.budget import fit_leading_hit
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
        if top_k <= 0 or (max_chars is not None and max_chars <= 0):
            return []
        if max_chars is None:
            return hits[:top_k]
        selected: list[SearchHit] = []
        remaining = max_chars
        for hit in hits:
            if len(selected) >= top_k:
                break
            if not selected:
                hit = fit_leading_hit(hit, remaining)
            if len(hit.content) <= remaining:
                selected.append(hit)
                remaining -= len(hit.content)
        return selected
