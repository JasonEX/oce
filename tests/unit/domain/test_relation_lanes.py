"""Pure relation helpers: qualified names, co-mentions, excerpts, section budgets."""

from oce.domain.services.evidence_pack import SectionInput, assemble_sections
from oce.domain.services.relations import RelatedOccurrence, occurrence_excerpt
from oce.domain.services.retrieval import (
    order_by_comentions,
    resolve_qualified_hits,
    split_qualified_identifiers,
)
from oce.domain.services.search import SearchHit


def _hit(path, content, start=1, context=None, blob="a" * 64):
    return SearchHit(
        blob_name=blob,
        path=path,
        content=content,
        score=1.0,
        content_hash="h" + path,
        start_line=start,
        end_line=start + len(content.splitlines()) - 1,
        context=context,
    )


def test_split_qualified_keeps_spelling_and_records_scope():
    lookup, qualifiers = split_qualified_identifiers(
        ("Session.get", "binding.Default", "plain", "a::b::C")
    )
    assert lookup == (
        "Session.get",
        "get",
        "binding.Default",
        "Default",
        "plain",
        "a::b::C",
        "C",
    )
    assert qualifiers == {"get": ("Session",), "Default": ("binding",), "C": ("b",)}


def test_qualified_resolution_prefers_scope_chain_then_path_then_text():
    method = _hit(
        "requests/sessions.py", "def get(self, url):", context="class Session"
    )
    module_function = _hit("requests/api.py", "def get(url, **kw):")
    resolved = resolve_qualified_hits([module_function, method], {"get": ("Session",)})
    assert resolved == [method]

    go_method = _hit("binding/binding.go", "func Default(method string) Binding {")
    other = _hit("render/render.go", "func Default() {")
    assert resolve_qualified_hits([other, go_method], {"Default": ("binding",)}) == [
        go_method
    ]

    receiver = _hit("context.go", "func (c *Context) ShouldBindJSON(obj any) error {")
    helper = _hit("binding/json.go", "func ShouldBindJSON() {}")
    assert resolve_qualified_hits(
        [helper, receiver], {"ShouldBindJSON": ("Context",)}
    ) == [receiver]
    # An unknown scope keeps every candidate rather than answering nothing.
    assert resolve_qualified_hits(
        [helper, receiver], {"ShouldBindJSON": ("Nope",)}
    ) == [
        helper,
        receiver,
    ]


def test_comention_order_picks_the_overload_naming_the_parameter_types():
    string_overload = _hit(
        "Gson.java", "public <T> T fromJson(String json, Class<T> c)"
    )
    reader_overload = _hit(
        "Gson.java",
        "public <T> T fromJson(JsonReader reader, TypeToken<T> t)",
        start=40,
    )
    ordered = order_by_comentions(
        [string_overload, reader_overload], ["JsonReader", "TypeToken"]
    )
    assert ordered[0] is reader_overload
    # Without other names the order is untouched.
    assert order_by_comentions([string_overload, reader_overload], []) == [
        string_overload,
        reader_overload,
    ]


def test_occurrence_excerpt_starts_at_the_enclosing_header():
    chunk = _hit(
        "src/api.py",
        "import x\n\n\ndef create(request):\n    a = 1\n    b = 2\n    total = build(request)\n    return total\n",
        start=10,
    )
    occurrence = RelatedOccurrence(
        "build", "call", chunk, line=16, end_line=16, enclosing="create"
    )
    excerpt = occurrence_excerpt(occurrence, 10, "caller")
    assert excerpt is not None
    assert excerpt.start_line == 13
    assert excerpt.content.startswith("def create(request):")
    assert "build(request)" in excerpt.content
    assert excerpt.role == "caller"

    # A header too far above the call falls out; the call line stays.
    far = RelatedOccurrence(
        "build", "call", chunk, line=16, end_line=16, enclosing="create"
    )
    short = occurrence_excerpt(far, 2, "caller")
    assert short is not None
    assert short.content.startswith("    total = build(request)")
    assert short.start_line == 16


def test_sections_dedupe_against_primary_and_respect_budgets():
    primary = _hit("src/a.py", "def a():\n    pass", start=1)
    same_span = RelatedOccurrence(
        "a", "call", _hit("src/a.py", "def a():\n    pass", start=1), 1, 1, "a"
    )
    caller_chunk = _hit("src/b.py", "def b():\n    a()\n", start=5, blob="b" * 64)
    caller = RelatedOccurrence("a", "call", caller_chunk, 6, 6, "b")
    test_chunk = _hit(
        "tests/test_a.py", "def test_a():\n    a()\n", start=1, blob="c" * 64
    )
    test = RelatedOccurrence("a", "call", test_chunk, 2, 2, "test_a")

    pack = assemble_sections(
        selected=[primary],
        related=[],
        sections=[
            SectionInput("caller", [same_span, caller], 4, 2_000),
            SectionInput("test", [test], 3, 250),
        ],
        remaining_chars=10_000,
        snippet_lines=10,
    )
    assert [(hit.role, hit.path) for hit in pack.hits] == [
        ("caller", "src/b.py"),
        ("test", "tests/test_a.py"),
    ]
    assert pack.counts == {"caller": 1, "test": 1}
    assert pack.chars == sum(len(hit.content) for hit in pack.hits)

    starved = assemble_sections(
        selected=[primary],
        related=[],
        sections=[SectionInput("test", [test], 3, 250)],
        remaining_chars=100,
        snippet_lines=10,
    )
    assert starved.hits == [] and starved.counts == {}
