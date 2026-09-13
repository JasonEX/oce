"""Character budgets shared by selection and expansion."""

from __future__ import annotations

from oce.domain.services.retrieval.state import RetrievalState
from oce.domain.services.selector.protocols import SelectionMode
from oce.shared.config.settings import RetrievalSettings

# Below this many characters a relation excerpt fragments into signatures
# that cost another SQL lookup without explaining a relationship.
MIN_RELATED_BUDGET = 1_000


def context_budget(settings: RetrievalSettings, state: RetrievalState) -> int:
    """The hard character budget the request's selection mode allows."""
    if state.strategy.selection_mode == SelectionMode.FOCUSED:
        return settings.focused_max_context_chars
    return settings.max_context_chars


def selected_chars(state: RetrievalState) -> int:
    return sum(len(hit.content) for hit in state.selected)
