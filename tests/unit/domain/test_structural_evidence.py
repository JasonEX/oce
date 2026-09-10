"""Qualified use sites, declaration signatures and traceback anchors."""

from __future__ import annotations

import pytest

from oce.domain.services.query_classifier import QueryIntent, QueryRoute
from oce.domain.services.query_evidence import QueryFrame, extract_query_evidence
from oce.domain.services.retrieval_strategy import plan_rerank
from oce.domain.services.search import (
    DefinitionHit,
    SearchHit,
)
from oce.domain.services.symbol_resolution import (
    _frame_matches,
    _signature_text,
    order_by_signature_comentions,
    resolve_qualified_definitions,
    resolve_qualified_hits,
)


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


def test_a_namespace_import_does_not_qualify_an_unrelated_same_name_definition():
    helper = _hit("tools/debug.py", "import engine\n\ndef start(): pass")

    assert resolve_qualified_hits([helper], {"start": ("engine",)}, strict=True) == []


def test_module_qualified_reference_accepts_a_use_inside_that_module():
    from oce.domain.services.symbol_resolution import mentions_requested_name

    local_use = _hit("binding/binding_test.go", "Default(method, contentType)")
    neighbour = _hit("other/binding_extra.go", "Default(method, contentType)")

    assert mentions_requested_name(local_use, "binding.Default")
    assert not mentions_requested_name(neighbour, "binding.Default")


def test_dollar_prefix_is_part_of_a_complete_identifier():
    from oce.domain.services.symbol_resolution import mentions_requested_name

    hit = _hit("src/reader.js", "return $readPacket()")

    assert mentions_requested_name(hit, "$readPacket")
    assert not mentions_requested_name(hit, "readPacket")


def test_signature_window_stops_at_the_parameter_list():
    lines = [
        "  public <T> T fromJson(Reader json, TypeToken<T> typeOfT)",
        "      throws JsonIOException, JsonSyntaxException {",
        "    JsonReader jsonReader = newJsonReader(json);",
        "    T object = fromJson(jsonReader, typeOfT);",
    ]
    assert "JsonReader" not in _signature_text(lines)
    assert "TypeToken" in _signature_text(lines)


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
    assert _frame_matches(frame, indexed) is expected


def test_deterministic_requests_skip_adaptive_rerankers_but_not_always():
    skipped = plan_rerank(QueryIntent.SYMBOL, 10, has_exact_hits=True)
    assert skipped.route == "skip:exact_definition"
    forced = plan_rerank(
        QueryIntent.SYMBOL,
        10,
        has_exact_hits=True,
        dedicated_policy="always",
    )
    assert forced.dedicated is True


def test_test_name_distance_prefers_the_test_named_after_the_symbol():
    from oce.domain.services.ranking import _test_name_distance

    walker = _hit("tree_test.go", "func TestWalker(t *testing.T) {\n\tWalk(r, fn)\n}")
    inline = _hit(
        "tree_test.go",
        "func TestWalkInlineMiddlewaresAcrossSubrouter(t *testing.T) {\n\tWalk(r, fn)\n}",
        start=40,
    )
    header = _hit("tree_test.go", 'import (\n\t"testing"\n)', start=80)
    # ``TestWalker`` keeps only the ``test`` prefix and ``er`` beyond ``walk``.
    assert _test_name_distance(walker, ("Walk",)) == 6
    assert _test_name_distance(inline, ("Walk",)) > 6
    assert _test_name_distance(header, ("Walk",)) is None
    python = _hit("tests/test_variable.py", "def test_as_compatible_data(self):")
    assert _test_name_distance(python, ("as_compatible_data",)) == 4


def test_test_question_prefers_named_test_over_earlier_file_header():
    from oce.domain.services.ranking import (
        HeadEvidence,
        source_heads,
        source_priority_factor,
    )
    from oce.domain.services.search import search_hit_key

    header = _hit("a/walk_test.go", 'import "testing"', blob="a")
    named_test = _hit(
        "b/walk_test.go", "func TestWalker(t *testing.T) {\n\tWalk(r, fn)\n}", blob="b"
    )
    evidence = HeadEvidence(
        route=QueryRoute(QueryIntent.REFERENCE, tests_requested=True),
        identifiers=("Walk",),
        exact=[header, named_test],
        use_sites=[named_test],
    )
    assert source_heads(
        evidence,
        [header, named_test],
        source_priority_factor,
        slots=2,
        reference_fallback=True,
    ) == (search_hit_key(named_test), search_hit_key(header))
