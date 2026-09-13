"""Round-3 lanes: qualified use sites, signature windows, hub spellings, anchors."""

from __future__ import annotations

import pytest

from oce.domain.services.query_classifier import QueryIntent
from oce.domain.services.query_evidence import QueryFrame, extract_query_evidence
from oce.domain.services.retrieval import (
    RetrievalPipeline,
    RetrievalState,
    hub_spellings,
    order_by_signature_comentions,
    resolve_qualified_definitions,
    resolve_qualified_hits,
)
from oce.domain.services.retrieval.hubs import hub_heads
from oce.domain.services.retrieval.names import signature_text
from oce.domain.services.retrieval.rank import test_name_distance as name_distance
from oce.domain.services.retrieval.recall_exact import frame_matches
from oce.domain.services.retrieval_strategy import plan_rerank
from oce.domain.services.search import (
    DefinitionHit,
    HubDefinition,
    SearchHit,
    SearchScope,
)
from oce.shared.config.settings import RetrievalSettings


def _hit(path, content, start=1, context=None, blob=None):
    return SearchHit(
        blob_name=blob or ("b" * 60 + path[-4:]).ljust(64, "x"),
        path=path,
        content=content,
        score=1.0,
        content_hash="h" + path + str(start),
        start_line=start,
        end_line=start + len(content.splitlines()) - 1,
        context=context,
    )


def test_qualified_declaration_line_pins_prototype_assignment():
    app_render = _hit(
        "lib/application.js", "app.render = function render(name, options, cb) {"
    )
    res_render = _hit("lib/response.js", "res.render = function render(view, opts) {")
    test_helper = _hit("test/app.engine.js", "function render(path, options, fn) {")
    resolved = resolve_qualified_hits(
        [test_helper, res_render, app_render], {"render": ("app",)}
    )
    # The test file's name ``app.engine`` is not the scope ``app``; the
    # declaration line that names both ``app`` and ``render`` is.
    assert resolved == [app_render]


def test_qualified_use_sites_keep_structural_and_textual_evidence():
    call = _hit("lib/response.js", "  app.render(view, opts, done);")
    scoped = _hit(
        "test/app.render.js", "  request(app).get('/')", context="describe('app')"
    )
    unrelated = _hit("examples/ejs/index.js", "  res.render('users', {})")
    resolved = resolve_qualified_hits(
        [unrelated, call, scoped], {"render": ("app",)}, declarations=False
    )
    assert resolved == [call, scoped]


def test_qualified_definitions_prefer_the_recorded_enclosing():
    router = DefinitionHit(
        "route",
        "definition",
        _hit("axum/src/routing/mod.rs", "pub fn route("),
        164,
        168,
        "Router",
    )
    path_router = DefinitionHit(
        "route",
        "definition",
        _hit("axum/src/routing/path_router.rs", "pub(super) fn route("),
        42,
        83,
        "PathRouter",
    )
    resolved = resolve_qualified_definitions(
        [path_router, router], {"route": ("Router",)}
    )
    assert resolved == [router]


def test_signature_window_stops_at_the_parameter_list():
    lines = [
        "  public <T> T fromJson(Reader json, TypeToken<T> typeOfT)",
        "      throws JsonIOException, JsonSyntaxException {",
        "    JsonReader jsonReader = newJsonReader(json);",
        "    T object = fromJson(jsonReader, typeOfT);",
    ]
    assert "JsonReader" not in signature_text(lines)
    assert "TypeToken" in signature_text(lines)


def test_overload_order_uses_the_parameter_list_only():
    reader_chunk = _hit(
        "Gson.java",
        "  public <T> T fromJson(Reader json, TypeToken<T> typeOfT)\n"
        "      throws JsonIOException {\n"
        "    JsonReader jsonReader = newJsonReader(json);\n"
        "    return fromJson(jsonReader, typeOfT);\n"
        "  }",
        start=1259,
    )
    json_reader_chunk = _hit(
        "Gson.java",
        "  public <T> T fromJson(JsonReader reader, TypeToken<T> typeOfT)\n"
        "      throws JsonIOException {\n"
        "    boolean isEmpty = true;\n"
        "  }",
        start=1345,
    )
    definitions = [
        DefinitionHit("fromJson", "definition", reader_chunk, 1259, 1263),
        DefinitionHit("fromJson", "definition", json_reader_chunk, 1345, 1348),
    ]
    ordered = order_by_signature_comentions(
        [reader_chunk, json_reader_chunk], definitions, ["JsonReader", "TypeToken"]
    )
    assert ordered[0] is json_reader_chunk


def test_hub_spellings_join_content_words_in_every_case():
    spellings = hub_spellings("How does pylint register a checker's messages?")
    assert {"register_checker", "registerChecker", "RegisterChecker"} <= set(spellings)
    assert {"Checker", "checker", "Message", "message"} <= set(spellings)
    # Stopwords and short words never become spellings.
    assert "how" not in spellings and "does" not in spellings


def test_hub_spellings_keep_mentions_and_identifiers_first():
    evidence = extract_query_evidence(
        "Explain the flow from Engine.ServeHTTP to IntoResponse conversion"
    )
    # A qualified spelling is a routing identifier; the CamelCase type the
    # prose mentions is not, so it is recorded as a mention instead.
    assert evidence.identifiers == ("Engine.ServeHTTP",)
    assert evidence.mentions == ("IntoResponse",)
    spellings = hub_spellings("x", evidence.mentions, ("ServeHTTP",))
    assert spellings[:2] == ("ServeHTTP", "IntoResponse")


def test_frames_carry_lines_and_ipython_style_frames_are_read():
    evidence = extract_query_evidence(
        'File "/site-packages/requests/adapters.py", line 292, in send\n'
        "    timeout=timeout\n"
        "File ~/env/lib/python3.9/site-packages/xarray/core/dataset.py:2110, in Dataset.chunks(self)\n"
    )
    assert evidence.frames == (
        QueryFrame("requests/adapters.py", "send", 292),
        QueryFrame("xarray/core/dataset.py", "chunks", 2110),
    )


def test_node_frames_keep_functions_after_async_and_new_prefixes():
    evidence = extract_query_evidence(
        "at async Session.get (/app/src/session.ts:12:5)\n"
        "at new Router (/app/src/router.js:34:2)\n"
    )
    assert evidence.frames == (
        QueryFrame("app/src/session.ts", "get", 12),
        QueryFrame("app/src/router.js", "Router", 34),
    )


@pytest.mark.parametrize(
    ("frame", "indexed", "expected"),
    [
        ("requests/sessions.py", "requests/sessions.py", True),
        ("site/requests/sessions.py", "requests/sessions.py", True),
        ("sessions.py", "requests/sessions.py", True),
        ("tests/sessions.py", "requests/sessions.py", False),
        ("requests/sessions.py", "requests/api.py", False),
    ],
)
def test_frame_path_matching_is_component_aligned(frame, indexed, expected):
    assert frame_matches(frame, indexed) is expected


def test_deterministic_requests_skip_adaptive_rerankers_but_not_always():
    skipped = plan_rerank(
        QueryIntent.REFERENCE, 10, has_exact_hits=True, dense_skipped=True
    )
    assert skipped.route == "skip:deterministic"
    forced = plan_rerank(
        QueryIntent.REFERENCE,
        10,
        has_exact_hits=True,
        dense_skipped=True,
        dedicated_policy="always",
    )
    assert forced.dedicated is True


class _Store:
    async def find_definitions(
        self, *, identifiers, scope, max_per_identifier=3, enclosing=None
    ):
        return []


def _pipeline(**settings):
    class Embedder:
        async def embed_query(self, text):
            return [0.0]

    class Search:
        async def search(self, **kwargs):
            return []

    return RetrievalPipeline(
        embedder=Embedder(),
        store=Search(),
        settings=RetrievalSettings(**settings),
        exact_store=_Store(),
    )


def test_hub_heads_skip_packages_unreferenced_names_and_tests():
    pipeline = _pipeline(hub_head_slots=2)
    state = RetrievalState(query="explain routing", scope=SearchScope(frozenset({"a"})))
    state.intent = QueryIntent.OVERVIEW
    package = HubDefinition(
        "routing",
        (
            DefinitionHit(
                "routing",
                "definition",
                _hit("axum/src/lib.rs", "pub mod routing;"),
                1,
                1,
            ),
        ),
        130,
        True,
    )
    router = HubDefinition(
        "Router",
        (
            DefinitionHit(
                "Router",
                "definition",
                _hit("axum/src/routing/mod.rs", "pub struct Router<S>"),
                1,
                40,
            ),
            DefinitionHit(
                "Router",
                "definition",
                _hit("axum/src/routing/mod.rs", "type Router = ()", start=90),
                90,
                90,
            ),
        ),
        115,
        False,
    )
    test_only = HubDefinition(
        "Fixture",
        (
            DefinitionHit(
                "Fixture",
                "definition",
                _hit("tests/fixture.rs", "struct Fixture"),
                1,
                3,
            ),
        ),
        4,
        False,
    )
    unreferenced = HubDefinition(
        "Orphan",
        (
            DefinitionHit(
                "Orphan", "definition", _hit("src/orphan.rs", "struct Orphan"), 1, 3
            ),
        ),
        0,
        False,
    )
    state.hubs = [package, router, test_only, unreferenced]
    heads = hub_heads(
        state.hubs, pipeline.priority_factor, pipeline.settings.hub_head_slots
    )
    assert [(hit.path, hit.start_line) for hit in heads] == [
        ("axum/src/routing/mod.rs", 1)
    ]


def test_strict_qualified_resolution_yields_nothing_for_unknown_scopes():
    outcomes = _hit(
        "src/_pytest/outcomes.py", "def skip(reason: str = '') -> NoReturn:"
    )
    assert resolve_qualified_hits([outcomes], {"skip": ("unittest",)}) == [outcomes]
    assert (
        resolve_qualified_hits([outcomes], {"skip": ("unittest",)}, strict=True) == []
    )


def test_test_name_distance_prefers_the_test_named_after_the_symbol():
    walker = _hit("tree_test.go", "func TestWalker(t *testing.T) {\n\tWalk(r, fn)\n}")
    inline = _hit(
        "tree_test.go",
        "func TestWalkInlineMiddlewaresAcrossSubrouter(t *testing.T) {\n\tWalk(r, fn)\n}",
        start=40,
    )
    header = _hit("tree_test.go", 'import (\n\t"testing"\n)', start=80)
    # ``TestWalker`` keeps only the ``test`` prefix and ``er`` beyond ``walk``.
    assert name_distance(walker, ("Walk",)) == 6
    assert name_distance(inline, ("Walk",)) > 6
    assert name_distance(header, ("Walk",)) is None
    python = _hit("tests/test_variable.py", "def test_as_compatible_data(self):")
    assert name_distance(python, ("as_compatible_data",)) == 4


@pytest.mark.asyncio
async def test_test_question_prefers_named_test_over_earlier_file_header():
    pipeline = _pipeline(source_head_slots=2)
    state = RetrievalState(
        query="Which tests cover Walk?", scope=SearchScope(frozenset({"a", "b"}))
    )
    state.intent = QueryIntent.REFERENCE
    state.lookup_identifiers = ("Walk",)
    header = _hit("a/walk_test.go", 'import "testing"', blob="a")
    named_test = _hit(
        "b/walk_test.go",
        "func TestWalker(t *testing.T) {\n\tWalk(r, fn)\n}",
        blob="b",
    )
    state.exact = [header, named_test]
    state.use_sites = [named_test]

    ranked = await pipeline.ranker.prefer_source_head(
        state, [header, named_test], pipeline.priority_factor
    )

    assert ranked == [named_test, header]
