"""Result selector protocol."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from oce.domain.services.search import SearchHit


class SelectionMode(StrEnum):
    FOCUSED = "focused"
    COVERAGE = "coverage"


class Selector(Protocol):
    async def select(
        self,
        hits: list[SearchHit],
        top_k: int,
        *,
        mode: SelectionMode = SelectionMode.COVERAGE,
    ) -> list[SearchHit]: ...
