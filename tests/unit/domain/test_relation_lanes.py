"""Pure relation helpers: qualified names, co-mentions, excerpts, section budgets."""

from oce.domain.services.evidence_pack import SectionInput, assemble_sections
from oce.domain.services.formatter import format_retrieval
from oce.domain.services.query_classifier import QueryIntent
from oce.domain.services.relations import RelatedOccurrence, occurrence_excerpt
from oce.domain.services.retrieval import (
    RetrievalPipeline,
    RetrievalState,
    get_strategy,
    order_by_comentions,
    resolve_qualified_hits,
    split_qualified_identifiers,
)
from oce.domain.services.search import DefinitionHit, SearchHit, SearchScope
from oce.shared.config.settings import RetrievalSettings


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


def test_qualified_candidate_filter_drops_unrelated_bare_name_hits():
    pipeline = RetrievalPipeline(
        embedder=object(), store=object(), settings=RetrievalSettings()
    )
    state = RetrievalState(
        query="where is Flask.make_response defined?",
        scope=None,
        intent=QueryIntent.SYMBOL,
        qualifiers={"make_response": ("Flask",)},
    )
    method = _hit(
        "src/flask/app.py", "def make_response(self, rv):", context="class Flask"
    )
    helper = _hit("src/flask/helpers.py", "def make_response(*args):")

    assert pipeline._filter_qualified_candidates(state, [helper, method]) == [method]
    unknown = RetrievalState(
        query="where is Unknown.make_response defined?",
        scope=None,
        intent=QueryIntent.SYMBOL,
        qualifiers={"make_response": ("Unknown",)},
    )
    assert pipeline._filter_qualified_candidates(unknown, [helper, method]) == [
        helper,
        method,
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


def test_relation_section_selection_prefers_novel_source_context():
    primary = _hit("src/a.py", "def a():\n    pass", start=1)
    same_file = RelatedOccurrence(
        "a",
        "call",
        _hit("src/a.py", "def local():\n    a()", start=10, blob="a" * 64),
        11,
        11,
        "local",
    )
    new_file = RelatedOccurrence(
        "a",
        "call",
        _hit("src/b.py", "def external():\n    a()", start=10, blob="c" * 64),
        11,
        11,
        "external",
    )

    pack = assemble_sections(
        selected=[primary],
        related=[],
        sections=[SectionInput("caller", [same_file, new_file], 1, 2_000)],
        remaining_chars=10_000,
        snippet_lines=10,
    )

    assert [hit.path for hit in pack.hits] == ["src/b.py"]


async def test_call_chain_expands_only_through_unique_definitions():
    class RelationStore:
        async def find_callers(self, *, identifiers, scope, limit=8):
            values = {
                "target": [
                    RelatedOccurrence(
                        "target",
                        "call",
                        _hit(
                            "src/dispatch.py",
                            "def dispatch():\n    target()",
                            blob="b" * 64,
                        ),
                        2,
                        2,
                        "dispatch",
                    )
                ],
                "dispatch": [
                    RelatedOccurrence(
                        "dispatch",
                        "call",
                        _hit(
                            "src/main.py", "def main():\n    dispatch()", blob="c" * 64
                        ),
                        2,
                        2,
                        "main",
                    )
                ],
            }
            return [
                item
                for identifier in identifiers
                for item in values.get(identifier, ())
            ][:limit]

    class ExactStore:
        async def find_definitions(self, *, identifiers, scope, max_per_identifier=3):
            return [
                DefinitionHit(
                    identifier=identifier,
                    kind="definition",
                    hit=_hit(f"src/{identifier}.py", f"def {identifier}():"),
                    start_line=1,
                    end_line=1,
                )
                for identifier in identifiers
                if identifier == "dispatch"
            ]

    pipeline = RetrievalPipeline(
        embedder=object(),
        store=object(),
        exact_store=ExactStore(),
        relation_store=RelationStore(),
        settings=RetrievalSettings(call_chain_max_hops=2, callers_max=4),
    )
    state = RetrievalState(
        query="trace target",
        scope=SearchScope(frozenset({"a" * 64})),
        intent=QueryIntent.CALL_CHAIN,
        strategy=get_strategy(QueryIntent.CALL_CHAIN),
    )

    occurrences = await pipeline._call_chain_callers(
        state, ("target",), state.scope, pipeline.relation_store
    )
    assert [(item.hit.path, item.hop) for item in occurrences] == [
        ("src/dispatch.py", 1),
        ("src/main.py", 2),
    ]
    excerpts = []
    for item in occurrences:
        excerpt = occurrence_excerpt(item, 10, "caller")
        if excerpt is not None:
            excerpts.append(excerpt)
    text = format_retrieval(excerpts)
    assert "Hop: 1" in text and "Hop: 2" in text
