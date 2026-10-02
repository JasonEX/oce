"""Every production selector respects the same code-character boundary."""

from __future__ import annotations

import pytest

from oce.domain.services.search import SearchHit, search_hit_key
from oce.domain.services.selector.coverage_selector import CoverageSelector
from oce.domain.services.selector.protocols import Selector
from oce.domain.services.selector.topk_selector import TopKSelector


@pytest.fixture(params=[CoverageSelector, TopKSelector])
def selector(request: pytest.FixtureRequest) -> Selector:
    return request.param()


async def test_oversized_leading_answer_keeps_complete_lines(
    selector: Selector,
) -> None:
    hit = SearchHit(
        "blob",
        "src/a.py",
        "first\nsecond\nthird",
        0.9,
        start_line=7,
        end_line=9,
        context="class Example",
    )

    selected = await selector.select([hit], 1, max_chars=14)

    assert selected[0].content == "first\nsecond"
    assert selected[0].start_line == 7
    assert selected[0].end_line == 8
    assert selected[0].context == hit.context
    assert len(selected[0].content) <= 14
    assert hit.content == "first\nsecond\nthird"


async def test_single_long_line_is_bounded_in_unicode_characters(
    selector: Selector,
) -> None:
    hit = SearchHit("blob", "src/a.py", "λ🙂" * 30, 0.9, start_line=3, end_line=3)

    selected = await selector.select([hit], 1, max_chars=5)

    assert selected[0].content == "λ🙂λ🙂λ"
    assert selected[0].end_line == 3


@pytest.mark.parametrize(
    ("content", "budget", "expected"),
    [
        ("first\nsecond\nthird", 12, "first\nsecond"),
        ("first\r\nsecond\r\nthird", 13, "first\r\nsecond"),
        ("first\r\nsecond\r\nthird", 14, "first\r\nsecond"),
    ],
)
async def test_exact_complete_line_boundary_is_preserved(
    selector: Selector, content: str, budget: int, expected: str
) -> None:
    hit = SearchHit("blob", "src/a.py", content, 0.9, start_line=7, end_line=9)

    selected = await selector.select([hit], 1, max_chars=budget)

    assert selected[0].content == expected
    assert selected[0].end_line == 8


async def test_smaller_later_answer_fits_when_a_tail_chunk_does_not(
    selector: Selector,
) -> None:
    hits = [
        SearchHit("a", "src/a.py", "12", 0.9),
        SearchHit("b", "src/b.py", "too large", 0.8),
        SearchHit("c", "src/c.py", "3456", 0.7),
    ]

    selected = await selector.select(hits, 2, max_chars=6)

    assert [hit.blob_name for hit in selected] == ["a", "c"]
    assert sum(len(hit.content) for hit in selected) == 6


async def test_zero_remaining_budget_returns_no_code(selector: Selector) -> None:
    hits = [SearchHit("a", "src/a.py", "code", 0.9)]
    assert await selector.select(hits, 1, max_chars=0) == []


async def test_protected_answers_keep_input_rank_order(selector: Selector) -> None:
    hits = [
        SearchHit("a", "src/a.py", "unprotected", 0.9),
        SearchHit("b", "src/b.py", "first head", 0.8),
        SearchHit("c", "src/c.py", "second head", 0.7),
    ]

    selected = await selector.select(
        hits,
        2,
        protected=(search_hit_key(hits[2]), search_hit_key(hits[1])),
    )

    assert selected == hits[1:]


async def test_protected_answers_share_character_budget(selector: Selector) -> None:
    hits = [
        SearchHit("a", "src/a.py", "123", 0.9),
        SearchHit("b", "src/b.py", "too large", 0.8),
        SearchHit("c", "src/c.py", "456", 0.7),
    ]

    selected = await selector.select(
        hits,
        3,
        max_chars=6,
        protected=tuple(search_hit_key(hit) for hit in hits[:2]),
    )

    assert selected == [hits[0], hits[2]]
    assert sum(len(hit.content) for hit in selected) == 6


async def test_oversized_protected_head_is_fitted_to_budget(selector: Selector) -> None:
    hits = [
        SearchHit("a", "src/a.py", "tail", 0.9),
        SearchHit("b", "src/b.py", "first\nsecond\nthird", 0.8, end_line=3),
    ]

    selected = await selector.select(
        hits, 2, max_chars=12, protected=(search_hit_key(hits[1]),)
    )

    assert len(selected) == 1
    assert selected[0].blob_name == "b"
    assert selected[0].content == "first\nsecond"
    assert selected[0].end_line == 2
