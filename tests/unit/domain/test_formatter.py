"""formatted_retrieval keeps the ACE section shape and adds context/related."""

from oce.domain.services.formatter import (
    CALLER_HEADER,
    HEADER,
    REEXPORT_HEADER,
    RELATED_HEADER,
    TEST_HEADER,
    format_retrieval,
)
from oce.domain.services.search import SearchHit


def _hit(path, content, start, *, context=None, role="primary"):
    return SearchHit(
        blob_name="a" * 64,
        path=path,
        content=content,
        score=1.0,
        start_line=start,
        end_line=start + len(content.splitlines()) - 1,
        context=context,
        role=role,
    )


def test_empty_result_is_header_only():
    assert format_retrieval([]) == HEADER


def test_context_line_and_related_section():
    text = format_retrieval(
        [
            _hit("src/a.py", "def run():\n    pass", 10, context="class Svc:"),
            _hit("src/b.py", "class Base:", 1, role="related"),
        ]
    )
    assert text.startswith(
        HEADER + "\nPath: src/a.py\nLines: 10-11\nContext: class Svc:\n"
    )
    assert "    10\tdef run():" in text
    assert RELATED_HEADER in text
    assert text.index(RELATED_HEADER) > text.index("src/a.py")
    assert "Path: src/b.py\nLines: 1-1\n     1\tclass Base:" in text


def test_relation_sections_render_in_fixed_order():
    text = format_retrieval(
        [
            _hit("src/a.py", "def run():\n    pass", 10),
            _hit("src/init.py", "from a import run", 1, role="reexport"),
            _hit("tests/test_a.py", "def test_run():", 3, role="test"),
            _hit("src/b.py", "def caller():\n    run()", 7, role="caller"),
            _hit("src/c.py", "class Base:", 1, role="related"),
        ]
    )
    positions = [
        text.index(header)
        for header in (RELATED_HEADER, CALLER_HEADER, TEST_HEADER, REEXPORT_HEADER)
    ]
    assert positions == sorted(positions)
    assert text.index("Path: src/a.py") < positions[0]
    assert "Path: src/init.py\nLines: 1-1" in text
