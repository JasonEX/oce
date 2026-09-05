"""Diversity- and budget-aware retrieval result selection."""

from __future__ import annotations

from collections import Counter

from oce.domain.services.search import SearchHit, SearchHitKey, search_hit_key
from oce.domain.services.selector.protocols import SelectionMode


class CoverageSelector:
    """Prefer repository coverage while suppressing overlapping source spans."""

    def __init__(
        self,
        *,
        max_per_path: int = 2,
        focused_max_per_path: int = 4,
        max_chars: int = 32_000,
        focused_max_chars: int = 12_000,
        overlap_threshold: float = 0.6,
    ) -> None:
        if max_per_path < 1:
            raise ValueError("max_per_path must be positive")
        if focused_max_per_path < 1:
            raise ValueError("focused_max_per_path must be positive")
        if max_chars < 1:
            raise ValueError("max_chars must be positive")
        if focused_max_chars < 1:
            raise ValueError("focused_max_chars must be positive")
        if not 0.0 <= overlap_threshold <= 1.0:
            raise ValueError("overlap_threshold must be between zero and one")
        self.max_per_path = max_per_path
        self.focused_max_per_path = focused_max_per_path
        self.max_chars = max_chars
        self.focused_max_chars = focused_max_chars
        self.overlap_threshold = overlap_threshold

    async def select(
        self,
        hits: list[SearchHit],
        top_k: int,
        *,
        mode: SelectionMode = SelectionMode.COVERAGE,
        max_chars: int | None = None,
    ) -> list[SearchHit]:
        if top_k <= 0 or not hits:
            return []

        selected: list[SearchHit] = []
        path_counts: Counter[str] = Counter()
        seen: set[SearchHitKey] = set()
        used_chars = 0

        passes: tuple[bool | None, ...]
        per_path_limit: int
        if mode == SelectionMode.FOCUSED:
            passes = (None,)
            per_path_limit = self.focused_max_per_path
            char_budget = self.focused_max_chars
        else:
            passes = (True, False)
            per_path_limit = self.max_per_path
            char_budget = self.max_chars
        if max_chars is not None:
            # A caller reserving room for relation sections lowers the budget;
            # it can never raise it above the configured hard limit.
            char_budget = max(1, min(char_budget, max_chars))

        # Coverage 先让不同文件各有代表，再补同文件片段；focused 严格保留
        # relevance 顺序。两种模式共用重叠抑制和字符预算。
        for prefer_new_path in passes:
            for hit in hits:
                # 达到数量上限：继续尝试（可能有更小的片段能塞进预算）
                if len(selected) >= top_k:
                    continue

                if prefer_new_path is not None and prefer_new_path != (
                    path_counts[hit.path] == 0
                ):
                    continue
                if path_counts[hit.path] >= per_path_limit:
                    continue
                key = search_hit_key(hit)
                if key in seen or self._overlaps_selected(hit, selected):
                    continue

                # 字符预算检查：放不下就跳过，继续尝试后面的小片段
                hit_chars = len(hit.content)
                if selected and used_chars + hit_chars > char_budget:
                    continue

                selected.append(hit)
                seen.add(key)
                path_counts[hit.path] += 1
                used_chars += hit_chars
        return selected

    def _overlaps_selected(
        self,
        candidate: SearchHit,
        selected: list[SearchHit],
    ) -> bool:
        for hit in selected:
            if hit.path != candidate.path:
                continue
            overlap = max(
                0,
                min(hit.end_line, candidate.end_line)
                - max(hit.start_line, candidate.start_line)
                + 1,
            )
            shorter = min(
                hit.end_line - hit.start_line + 1,
                candidate.end_line - candidate.start_line + 1,
            )
            if overlap / shorter >= self.overlap_threshold:
                return True
        return False
