"""Pure relation helpers: qualified names, co-mentions, excerpts, section budgets."""

from dataclasses import replace

import pytest

from oce.application.call_chain import resolve_endpoints, trace_callers, trace_path
from oce.application.retrieval import (
    RetrievalPipeline,
    RetrievalState,
    get_strategy,
)
from oce.domain.services.evidence_pack import SectionInput, assemble_sections
from oce.domain.services.formatter import format_retrieval
from oce.domain.services.query_classifier import QueryIntent, QueryRoute
from oce.domain.services.relations import RelatedOccurrence, occurrence_excerpt
from oce.domain.services.search import DefinitionHit, SearchHit, SearchScope
from oce.domain.services.symbol_resolution import (
    order_by_comentions,
    order_by_signature_comentions,
    resolve_qualified_hits,
    split_qualified_identifiers,
)
from oce.shared.config.settings import RetrievalSettings
from tests.unit.application.fakes import EmptyExactSearchStore


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


@pytest.mark.parametrize("path", ["src/local.py", "src/__init__.py"])
async def test_implementation_request_reuses_inherit_facts_after_selection(path):
    local = _hit(path, "class LocalDriver(SocketDriver):\n    pass")
    remote = _hit(
        "src/remote.py", "class RemoteDriver(SocketDriver):\n    pass", blob="b" * 64
    )
    factory = _hit(
        "src/factory.py", "def make():\n    return SocketDriver()", blob="c" * 64
    )

    class Implementations:
        lookups = 0

        async def find_implementations(self, *, identifiers, scope, limit=8):
            self.lookups += 1
            assert identifiers == ("SocketDriver",)
            return [
                RelatedOccurrence(
                    "SocketDriver", "inherit", local, 1, 2, "LocalDriver"
                ),
                RelatedOccurrence(
                    "SocketDriver", "inherit", remote, 1, 2, "RemoteDriver"
                ),
            ]

    relations = Implementations()
    pipe = RetrievalPipeline(
        embedder=object(),
        store=object(),
        relation_store=relations,
        settings=RetrievalSettings(
            final_select_k=1,
            callers_enabled=False,
            tests_enabled=False,
            related_definitions_enabled=False,
        ),
    )
    state = RetrievalState(
        query="Which classes implement `SocketDriver`?",
        scope=SearchScope(
            frozenset({local.blob_name, remote.blob_name, factory.blob_name})
        ),
        candidates=[factory, local],
        # Dense recalled the class; the bounded implementation lookup proves
        # its role even when it fell outside the general exact recall window.
        exact=[factory],
        use_sites=[factory],
    )
    pipe._route(state)

    await pipe._rank(state)
    await pipe._select(state)
    await pipe._expand(state)

    assert state.selected == [local]
    assert [(hit.path, hit.role) for hit in state.related] == [
        (remote.path, "implementation")
    ]
    assert relations.lookups == 1


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
        route=QueryRoute(QueryIntent.SYMBOL),
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
        route=QueryRoute(QueryIntent.SYMBOL),
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

    class ExactStore(EmptyExactSearchStore):
        async def find_definitions(
            self, *, identifiers, scope, max_per_identifier=3, enclosing=None
        ):
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
        route=QueryRoute(QueryIntent.CALL_CHAIN),
        strategy=get_strategy(QueryIntent.CALL_CHAIN),
    )

    occurrences = await trace_callers(
        pipeline.relation_store,
        pipeline.exact_store,
        ("target",),
        state.scope,
        max_hops=2,
        max_callers=4,
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


def test_signature_comentions_pick_the_overload_declared_with_the_named_types():
    # Both chunks mention JsonReader and TypeToken somewhere (javadoc); only
    # the second declares the overload that takes them.
    reader_doc = _hit(
        "Gson.java",
        "/** Like fromJson(JsonReader, TypeToken) but for strings. */\n"
        "public <T> T fromJson(String json, Class<T> classOfT) {\n  return null;\n}",
        start=100,
    )
    reader_impl = _hit(
        "Gson.java",
        "/** Reads JSON. */\n"
        "public <T> T fromJson(JsonReader reader, TypeToken<T> typeOfT) {\n"
        "  return null;\n}",
        start=200,
    )
    definitions = [
        DefinitionHit("fromJson", "definition", reader_doc, 101, 103),
        DefinitionHit("fromJson", "definition", reader_impl, 201, 203),
    ]
    ordered = order_by_signature_comentions(
        [reader_doc, reader_impl], definitions, ["JsonReader", "TypeToken"]
    )
    assert [hit.start_line for hit in ordered] == [200, 100]
    # Without signature evidence the chunk-level count alone cannot tell.
    assert order_by_comentions([reader_doc, reader_impl], ["JsonReader"]) == [
        reader_doc,
        reader_impl,
    ]


async def test_two_endpoint_chain_renders_header_and_handover_window():
    """A hop whose delegating call sits deep in its body gets two excerpts.

    The header names the declaration the flow passes through; the window ends
    at the line that hands over to the next hop. Headers of every hop are
    placed before any window spends the chain budget.
    """
    body = (
        ["def main():"] + [f"    step_{i}()" for i in range(1, 14)] + ["    target()"]
    )
    body += ["    return 0"] * 10
    main_chunk = _hit("src/main.py", "\n".join(body), start=1, blob="a" * 64)
    target_chunk = _hit(
        "src/target.py", "def target():\n    return 1", start=1, blob="b" * 64
    )
    main = DefinitionHit("main", "definition", main_chunk, 1, len(body))
    target = DefinitionHit("target", "definition", target_chunk, 1, 2)

    class ExactStore(EmptyExactSearchStore):
        async def calls_within(self, *, blob_name, start_line, end_line, scope):
            return [("target", 15, "main")] if blob_name == "a" * 64 else []

        async def find_definitions(
            self, *, identifiers, scope, max_per_identifier=3, enclosing=None
        ):
            return [target] if "target" in identifiers else []

        async def chunk_for_line(self, *, blob_name, line, scope):
            return main_chunk if blob_name == "a" * 64 else None

    pipeline = RetrievalPipeline(
        embedder=object(),
        store=object(),
        exact_store=ExactStore(),
        settings=RetrievalSettings(related_snippet_lines=10),
    )
    state = RetrievalState(
        query="How does `main` reach `target`?",
        scope=SearchScope(frozenset({"a" * 64, "b" * 64})),
        route=QueryRoute(QueryIntent.CALL_CHAIN),
        strategy=get_strategy(QueryIntent.CALL_CHAIN),
        endpoints=[("main", [main]), ("target", [target])],
    )

    chain = await trace_path(
        pipeline.exact_store,
        state.scope,
        state.endpoints,
        state.selected,
        pipeline.settings,
    )

    assert [(hit.hop, hit.path, hit.start_line, hit.end_line) for hit in chain] == [
        (0, "src/main.py", 1, 10),
        (0, "src/main.py", 11, 15),
        (1, "src/target.py", 1, 2),
    ]
    assert chain[0].content.startswith("def main():")
    assert chain[1].content.splitlines()[-1].strip() == "target()"
    assert all(hit.role == "chain" for hit in chain)

    # A tight chain budget keeps every hop's header and drops the window.
    pipeline.settings = RetrievalSettings(
        related_snippet_lines=10,
        call_chain_max_chars=len(chain[0].content) + len(chain[2].content),
    )
    chain = await trace_path(
        pipeline.exact_store,
        state.scope,
        state.endpoints,
        state.selected,
        pipeline.settings,
    )
    assert [(hit.hop, hit.start_line) for hit in chain] == [(0, 1), (1, 1)]


async def test_unresolved_start_does_not_turn_the_target_into_a_trace_start():
    target_chunk = _hit("src/target.py", "def target():\n    return 1", blob="b" * 64)
    target = DefinitionHit("target", "definition", target_chunk, 1, 2)

    class ExactStore(EmptyExactSearchStore):
        async def find_definitions(
            self, *, identifiers, scope, max_per_identifier=3, enclosing=None
        ):
            return [target] if "target" in identifiers else []

    pipeline = RetrievalPipeline(
        embedder=object(),
        store=object(),
        exact_store=ExactStore(),
        settings=RetrievalSettings(),
    )
    state = RetrievalState(
        query="How does `missing_start` reach `target`?",
        scope=SearchScope(frozenset({"b" * 64})),
        route=QueryRoute(QueryIntent.CALL_CHAIN),
        strategy=get_strategy(QueryIntent.CALL_CHAIN),
    )

    endpoints = await resolve_endpoints(
        pipeline.exact_store, state.scope, ("missing_start", "target"), state.qualifiers
    )

    assert endpoints == []


async def test_chain_and_relation_sections_share_the_hard_context_budget(monkeypatch):
    primary = [
        _hit("src/first.py", "a" * 1_000, blob="a" * 64),
        _hit("src/second.py", "b" * 700, blob="b" * 64),
        _hit("src/third.py", "c" * 500, blob="c" * 64),
    ]
    chain_hit = replace(
        _hit("src/hop.py", "h" * 400, blob="d" * 64), role="chain", hop=1
    )
    caller_hit = _hit("src/caller.py", "x" * 600, blob="e" * 64)
    caller = RelatedOccurrence("start", "call", caller_hit, 1, 1, "caller")
    start = DefinitionHit("start", "definition", primary[0], 1, 1)

    class ExactStore(EmptyExactSearchStore):
        async def calls_within(self, *, blob_name, start_line, end_line, scope):
            return []

    class RelationStore:
        async def find_callers(self, *, identifiers, scope, limit=8):
            return [caller]

    async def fake_callees(
        store, scope, endpoints, selected, settings, *, max_chars=None
    ):
        assert max_chars is not None and len(chain_hit.content) <= max_chars
        return [chain_hit]

    monkeypatch.setattr("oce.application.retrieval.trace_callees", fake_callees)
    pipeline = RetrievalPipeline(
        embedder=object(),
        store=object(),
        exact_store=ExactStore(),
        relation_store=RelationStore(),
        settings=RetrievalSettings(
            max_context_chars=2_500,
            relation_reserve_chars=1_000,
            related_definitions_enabled=False,
            tests_enabled=False,
            merge_adjacent_enabled=False,
        ),
    )
    state = RetrievalState(
        query="Trace how `start` dispatches.",
        scope=SearchScope(frozenset({"a" * 64, "b" * 64, "c" * 64})),
        route=QueryRoute(QueryIntent.CALL_CHAIN),
        strategy=get_strategy(QueryIntent.CALL_CHAIN),
        lookup_identifiers=("start",),
        endpoints=[("start", [start])],
        selected=primary,
    )

    await pipeline._expand(state)

    hits = [*state.selected, *state.related]
    assert sum(len(hit.content) for hit in hits) <= 2_500
    assert {hit.role for hit in state.related} == {"chain", "caller"}


async def test_relation_room_reuses_facts_and_reconsiders_a_removed_primary_tail():
    from oce.domain.services.query_evidence import extract_query_evidence
    from tests.unit.application.fakes import EmptyExactSearchStore

    primary = _hit("src/main.py", "m" * 1600, blob="a" * 64)
    tail = _hit(
        "src/target.py",
        "def target():\n    " + "x" * 180 + "\n    return 1\n#" + "p" * 500,
        blob="b" * 64,
    )
    caller_hit = _hit(
        "src/caller.py",
        "def caller():\n    target()\n    # " + "c" * 320,
        blob="c" * 64,
    )
    caller = RelatedOccurrence("target", "call", caller_hit, 2, 1, "caller")

    class ExactStore(EmptyExactSearchStore):
        lookups = 0

        async def find_definitions(
            self, *, identifiers, scope, max_per_identifier=3, enclosing=None
        ):
            self.lookups += 1
            return [DefinitionHit("target", "definition", tail, 1, 3)]

    class RelationStore:
        async def find_callers(self, *, identifiers, scope, limit=8):
            return [caller]

    store = ExactStore()
    pipeline = RetrievalPipeline(
        embedder=object(),
        store=object(),
        exact_store=store,
        relation_store=RelationStore(),
        settings=RetrievalSettings(
            focused_max_context_chars=2500,
            relation_reserve_chars=1000,
            related_definitions_enabled=True,
            tests_enabled=False,
            implementations_enabled=False,
            reexports_enabled=False,
            merge_adjacent_enabled=False,
        ),
    )
    state = RetrievalState(
        query="Where is `target` defined?",
        scope=SearchScope(
            frozenset(hit.blob_name for hit in (primary, tail, caller_hit))
        ),
        route=QueryRoute(QueryIntent.SYMBOL, ("target",)),
        evidence=extract_query_evidence("Where is `target` defined?"),
        lookup_identifiers=("target",),
        strategy=get_strategy(QueryIntent.SYMBOL),
        selected=[primary, tail],
    )
    await pipeline._expand(state)
    assert store.lookups == 1
    assert state.selected == [primary]
    assert any(hit.role == "related" and hit.path == tail.path for hit in state.related)
    assert any(hit.role == "caller" for hit in state.related)
    assert sum(len(hit.content) for hit in (*state.selected, *state.related)) <= 2500
