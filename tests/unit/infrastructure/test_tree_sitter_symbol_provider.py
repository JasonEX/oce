"""tree-sitter definitions and imports across grammars, with regex fallback."""

from typing import NoReturn

import pytest
from tree_sitter import Parser

from oce.domain.services.symbols import SymbolOccurrence
from oce.infrastructure.astchunk.symbol_provider import (
    MAX_TREE_SITTER_BYTES,
    TreeSitterSymbolProvider,
)
from oce.infrastructure.regex_symbol_provider import RegexSymbolProvider


def _extract(language: str, content: str) -> set[tuple[str, str, int, int]]:
    provider = TreeSitterSymbolProvider(RegexSymbolProvider())
    return {
        (o.identifier, o.kind, o.start_line, o.end_line)
        for o in provider.extract(content=content, language=language)
    }


def test_python_definitions_imports_endpoints_and_locals():
    found = _extract(
        "python",
        "import os\n"
        "from oce.domain import Blob as Bl\n"
        "MAX = 3\n"
        '@app.get("/x")\n'
        "async def handler(req):\n"
        "    local_var = 1\n"
        "    def inner():\n"
        "        pass\n"
        "    return local_var\n"
        "class Service(Base):\n"
        "    attr = 2\n"
        "    def run(self, x):\n"
        "        return x\n",
    )
    assert ("handler", "endpoint", 5, 9) in found
    assert ("inner", "definition", 7, 8) in found
    assert ("Service", "definition", 10, 13) in found
    assert ("run", "definition", 12, 13) in found
    assert ("MAX", "definition", 3, 3) in found
    assert ("attr", "definition", 11, 11) in found
    assert ("os", "import", 1, 1) in found
    assert ("Blob", "import", 2, 2) in found and ("Bl", "import", 2, 2) in found
    assert not any(item[0] == "local_var" for item in found)


def test_other_grammars_use_fields_not_type_tables():
    go = _extract(
        "go",
        'import "net/http"\n'
        "type Server struct { port int }\n"
        "func (s *Server) Handle() { x := 1 }\n",
    )
    assert {
        ("Server", "definition", 2, 2),
        ("Handle", "definition", 3, 3),
        ("http", "import", 1, 1),
    } <= go

    rust = _extract(
        "rust",
        "use std::collections::HashMap;\n"
        "pub struct Config { pub name: String }\n"
        "impl Config { pub fn load() -> Self { let x = 1; Config { name: x.into() } } }\n",
    )
    assert {
        ("Config", "definition", 2, 2),
        ("load", "definition", 3, 3),
        ("HashMap", "import", 1, 1),
    } <= rust

    ts = _extract(
        "typescript",
        "import { useState } from 'react';\n"
        "export const handler = async () => { const local = 1; return local; };\n"
        "export default class Store { private count = 0; get(): number { return this.count; } }\n",
    )
    assert {
        ("handler", "definition", 2, 2),
        ("Store", "definition", 3, 3),
        ("get", "definition", 3, 3),
        ("useState", "import", 1, 1),
    } <= ts
    assert not any(item[0] == "local" for item in ts)

    kotlin = _extract(
        "kotlin",
        "class ViewModel(val repo: Repo) : Base() {\n    val state: Int = 1\n    fun load(): Int { val local = 2; return local }\n}\n",
    )
    assert {
        ("ViewModel", "definition", 1, 4),
        ("state", "definition", 2, 2),
        ("load", "definition", 3, 3),
    } <= kotlin
    assert not any(item[0] == "local" for item in kotlin)


def test_unknown_language_and_huge_file_fall_back_to_regex():
    provider = TreeSitterSymbolProvider(RegexSymbolProvider())
    found = provider.extract(content="def fallback_fn():\n    pass\n", language=None)
    assert [(o.identifier, o.kind) for o in found] == [("fallback_fn", "definition")]

    huge = "x = 1\n" * 200_000 + "def tail_fn():\n    pass\n"
    found = provider.extract(content=huge, language="python")
    assert any(o.identifier == "tail_fn" for o in found)


def test_multibyte_file_size_limit_is_measured_in_bytes():
    class RecordingFallback:
        called = False

        def extract(self, *, content: str, language: str | None):
            self.called = True
            return (SymbolOccurrence("fallback", "definition", 1, 1),)

    fallback = RecordingFallback()
    provider = TreeSitterSymbolProvider(fallback)
    content = "界" * (MAX_TREE_SITTER_BYTES // 3 + 1)

    found = provider.extract(content=content, language="python")

    assert fallback.called is True
    assert [occurrence.identifier for occurrence in found] == ["fallback"]


def test_bash_functions_and_commonjs_assignments_are_definitions():
    provider = TreeSitterSymbolProvider(RegexSymbolProvider())

    bash = provider.extract(
        content="#!/usr/bin/env bash\nbats_print_stack_trace() {\n  :\n}\nfunction skip {\n  :\n}\n",
        language="bash",
    )
    assert {(o.identifier, o.kind) for o in bash} == {
        ("bats_print_stack_trace", "definition"),
        ("skip", "definition"),
    }

    javascript = provider.extract(
        content=(
            "app.use = function use(fn) {}\n"
            "exports.query = function query(o) {}\n"
            "Layer.prototype.match = function match(p) {}\n"
            "module.exports = createApplication\n"
            "function createApplication() { var x = function inner() {}; app.settings = {}; }\n"
            "const handler = (req) => {}\n"
        ),
        language="javascript",
    )
    names = {o.identifier for o in javascript if o.kind == "definition"}
    # Prototype and CommonJS assignments define the API; the bare re-export,
    # the plain object assignment and the nested local function do not.
    assert names == {"createApplication", "use", "query", "match", "handler"}


def test_transient_grammar_failure_is_retried(monkeypatch):
    import oce.infrastructure.astchunk.symbol_provider as module

    calls = {"n": 0}
    real = module.get_parser

    def flaky(name):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("download hiccup")
        return real(name)

    monkeypatch.setattr(module, "get_parser", flaky)
    provider = TreeSitterSymbolProvider(RegexSymbolProvider())
    source = "class Foo:\n    pass\n"
    assert provider.extract(content=source, language="python")  # regex fallback
    second = provider.extract(content=source, language="python")
    assert calls["n"] == 2
    assert {(o.identifier, o.kind) for o in second} == {("Foo", "definition")}


def test_commonjs_require_aliases_are_imports_not_definitions():
    provider = TreeSitterSymbolProvider(RegexSymbolProvider())
    javascript = provider.extract(
        content=(
            "var Route = require('./router/route');\n"
            "var compileETag = require('./utils').compileETag;\n"
            "const { Layer, decorate } = require('./layer');\n"
            "var proto = module.exports = function(options) {};\n"
            "function Router() {}\n"
        ),
        language="javascript",
    )
    definitions = {o.identifier for o in javascript if o.kind == "definition"}
    imports = {o.identifier for o in javascript if o.kind == "import"}
    assert "Router" in definitions
    assert definitions.isdisjoint({"Route", "compileETag", "Layer", "decorate"})
    assert {"Route", "compileETag", "Layer", "decorate"} <= imports


def test_rust_trait_impls_and_let_bindings_are_not_definitions():
    rust = _extract(
        "rust",
        "pub struct Router<S> { inner: S }\n"
        "impl<S> Clone for Router<S> { fn clone(&self) -> Self { todo!() } }\n"
        "impl<S> Router<S> { pub fn new() -> Self { todo!() } }\n"
        "fn check() { let _: Router<()> = Router::new(); let app = Router::new(); }\n",
    )
    router_definitions = sorted(
        item[2] for item in rust if item[0] == "Router" and item[1] == "definition"
    )
    # The struct and the inherent impl; not the trait impl, not the let types.
    assert router_definitions == [1, 3]
    assert not any(item[0] == "app" and item[1] == "definition" for item in rust)
    assert ("clone", "definition", 2, 2) in rust
    assert ("new", "definition", 3, 3) in rust


def test_re_exports_and_prose_declare_nothing():
    provider = TreeSitterSymbolProvider(RegexSymbolProvider())
    ts = provider.extract(
        content=(
            "export { configureStore, type Foo } from './configureStore'\n"
            "export * from './x'\n"
            "export const local = 1\n"
        ),
        language="typescript",
    )
    definitions = {o.identifier for o in ts if o.kind == "definition"}
    assert "configureStore" not in definitions
    assert "local" in definitions
    prose = provider.extract(
        content="# Usage\n\n```python\ndef configure_store():\n    pass\n```\n",
        language="markdown",
    )
    assert prose == ()


def test_occurrences_record_their_enclosing_definition_and_heritage():
    provider = TreeSitterSymbolProvider(RegexSymbolProvider())
    found = {
        (o.kind, o.identifier, o.enclosing)
        for o in provider.extract(
            content=(
                "class Service(BaseService, Mixin, metaclass=ABCMeta):\n"
                "    def run(self):\n"
                "        return helper_fn(1)\n"
                "\n"
                "def top():\n"
                "    Service().run()\n"
            ),
            language="python",
        )
    }
    assert ("definition", "run", "Service") in found
    assert ("call", "helper_fn", "run") in found
    assert ("call", "run", "top") in found
    assert ("inherit", "BaseService", "Service") in found
    assert ("inherit", "Mixin", "Service") in found
    assert not any(kind == "inherit" and name == "ABCMeta" for kind, name, _ in found)

    rust = {
        (o.kind, o.identifier, o.enclosing)
        for o in provider.extract(
            content=(
                "impl IntoResponse for StatusCode {\n"
                "    fn into_response(self) -> Response { build(self) }\n"
                "}\n"
            ),
            language="rust",
        )
    }
    assert ("inherit", "IntoResponse", "StatusCode") in rust
    assert ("definition", "into_response", "StatusCode") in rust

    java = {
        (o.kind, o.identifier, o.enclosing)
        for o in provider.extract(
            content="public class Handler implements Factory<Ping>, Closeable {}\n",
            language="java",
        )
    }
    assert ("inherit", "Factory", "Handler") in java
    assert ("inherit", "Closeable", "Handler") in java
    assert not any(name == "Ping" for _, name, _ in java)


def test_barrel_files_and_pub_use_record_reexports():
    provider = TreeSitterSymbolProvider(RegexSymbolProvider())

    def reexports(content, language, path):
        return {
            o.identifier
            for o in provider.extract(content=content, language=language, path=path)
            if o.kind == "reexport"
        }

    python_init = "from .app import Flask as Flask\nfrom billing.tax import TaxTable\nimport os\nfrom other import Thing\n"
    assert reexports(python_init, "python", "src/billing/__init__.py") == {
        "Flask",
        "TaxTable",
    }
    # The same imports in an ordinary module publish nothing.
    assert reexports(python_init, "python", "src/billing/util.py") == set()

    ts_index = "export { createSlice, buildCreateSlice as build } from './createSlice'\nexport * from './x'\nimport { other } from './other'\n"
    assert reexports(ts_index, "typescript", "src/index.ts") == {"createSlice", "build"}
    assert reexports(ts_index, "typescript", "src/store.ts") == {"createSlice", "build"}

    rust_mod = "pub use self::from_fn::{from_fn, Next};\nuse std::collections::HashMap;\npub(crate) use a::Base as Alias;\n"
    assert reexports(rust_mod, "rust", "axum/src/middleware/mod.rs") == {
        "from_fn",
        "Next",
        "Alias",
    }


def test_macro_token_text_does_not_create_call_edges():
    provider = TreeSitterSymbolProvider(RegexSymbolProvider())
    source = (
        "fn entry() {\n"
        "    let _ = stringify!(remove_record());\n"
        "    custom_language!(literal_tokens(nested_tokens()));\n"
        "    actual_call();\n"
        "}\n"
    )
    calls = {
        (o.identifier, o.enclosing)
        for o in provider.extract(content=source, language="rust", path="src/lib.rs")
        if o.kind == "call"
    }
    assert calls == {("actual_call", "entry")}


def test_python_named_import_alias_preserves_both_use_site_names() -> None:
    provider = TreeSitterSymbolProvider(RegexSymbolProvider())
    source = (
        "from billing import charge as debit\n"
        "def entry():\n"
        "    debit()\n"
        "    remote.debit()\n"
    )
    calls = {
        (o.identifier, o.start_line, o.enclosing)
        for o in provider.extract(content=source, language="python")
        if o.kind == "call"
    }
    assert calls == {
        ("debit", 3, "entry"),
        ("charge", 3, "entry"),
        ("debit", 4, "entry"),
    }


@pytest.mark.parametrize(
    "conflict",
    [
        "debit = replacement\n",
        "def other(debit): pass\n",
        "def other():\n    debit = replacement\n",
        "def debit(): pass\n",
        "from other import replacement as debit\n",
        "from other import *\n",
        "def other():\n    global debit\n",
        "for debit in callbacks: pass\n",
        "with transaction() as debit: pass\n",
        "try: pass\nexcept Exception as debit: pass\n",
        "[debit for debit in callbacks]\n",
        "(debit := replacement)\n",
        "del debit\n",
        "type debit = int\n",
        "def other[debit](): pass\n",
    ],
)
def test_python_alias_origin_abstains_on_conflicting_bindings(conflict: str) -> None:
    provider = TreeSitterSymbolProvider(RegexSymbolProvider())
    source = (
        "from billing import charge as debit\n"
        + conflict
        + "def entry():\n    return debit()\n"
    )
    calls = [
        o
        for o in provider.extract(content=source, language="python")
        if o.kind == "call"
    ]
    assert not any(o.identifier == "charge" for o in calls)


def test_conditional_imports_and_incomplete_syntax_add_no_alias_origin() -> None:
    provider = TreeSitterSymbolProvider(RegexSymbolProvider())
    for source in (
        "if enabled:\n    from billing import charge as debit\n"
        "def entry():\n    return debit()\n",
        "from billing import charge as debit\ndef entry():\n    other(\n    debit()\n",
        "from billing import (charge as debit, @)\ndef entry():\n    return debit()\n",
        "from billing import charge as debit,, other\n"
        "def entry():\n    return debit()\n",
        "debit()\nfrom billing import charge as debit\n",
    ):
        calls = [
            o
            for o in provider.extract(content=source, language="python")
            if o.kind == "call"
        ]
        assert not any(o.identifier == "charge" for o in calls)


def test_custom_common_named_methods_do_not_enable_unrelated_receiver_calls() -> None:
    provider = TreeSitterSymbolProvider(RegexSymbolProvider())
    source = (
        "class Registry:\n"
        "    def get(self): pass\n"
        "    def fetch(self):\n"
        "        self.get()\n"
        "        remote.get()\n"
        "        mapping.get('key')\n"
        "def entry():\n"
        "    Registry.get(registry)\n"
        "    registry.get()\n"
        "    get()\n"
        "    len([])\n"
    )
    calls = {
        (o.identifier, o.start_line)
        for o in provider.extract(content=source, language="python")
        if o.kind == "call"
    }
    assert calls == {("get", 4), ("get", 8)}


def test_custom_bare_builtin_name_and_import_alias_are_retained() -> None:
    found = _extract(
        "python",
        "from reader import fetch_document as open\n"
        "def len(value): return 3\n"
        "def entry():\n"
        "    open()\n"
        "    len([])\n"
        "    mapping.get('key')\n",
    )
    assert {item for item in found if item[1] == "call"} == {
        ("open", "call", 4, 4),
        ("fetch_document", "call", 4, 4),
        ("len", "call", 5, 5),
    }


@pytest.mark.parametrize(
    "source",
    [
        "def get(): pass\ndef entry(get):\n    get()\n",
        "def get(): pass\ndef entry():\n    get = remote\n    get()\n",
        "class Registry:\n"
        "    def get(self): pass\n"
        "    def entry(self):\n"
        "        self = remote\n"
        "        self.get()\n",
        "class Registry:\n"
        "    def get(self): pass\n"
        "    @staticmethod\n"
        "    def entry(self):\n"
        "        self.get()\n",
        "class Registry:\n    def get(self): pass\nRegistry = remote\nRegistry.get()\n",
        "class Registry:\n"
        "    def get(self): pass\n"
        "def entry(Registry):\n"
        "    Registry.get()\n",
        "class Registry:\n"
        "    def get(self): pass\n"
        "    def entry(other):\n"
        "        self.get()\n",
    ],
)
def test_common_named_call_admission_abstains_on_shadowing(source: str) -> None:
    found = _extract("python", source)
    assert not any(item[0] == "get" and item[1] == "call" for item in found)


def test_receiver_attribute_stores_and_annotations_do_not_rebind_the_receiver() -> None:
    found = _extract(
        "python",
        "class Registry:\n"
        "    def get(self): pass\n"
        "    def entry(self: Registry):\n"
        "        self.values = {}\n"
        "        self.values['key'] = 1\n"
        "        self.get()\n",
    )
    assert ("get", "call", 6, 6) in found


@pytest.mark.parametrize("language", ["javascript", "typescript"])
def test_common_name_admission_requires_supported_binding_analysis(
    language: str,
) -> None:
    found = _extract(
        language,
        "function get() { return 1; }\n"
        "function entry(get) { return get(); }\n"
        "class Registry { get() { return 1; } }\n"
        "function run(Registry) { return Registry.get(); }\n",
    )
    assert not any(item[0] == "get" and item[1] == "call" for item in found)


def test_parse_exception_fallback_does_not_create_alias_call_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import oce.infrastructure.astchunk.symbol_provider as module

    def fail_parse(parser: Parser, source: str) -> NoReturn:
        raise ValueError("invalid parser")

    monkeypatch.setattr(module, "compat_parse", fail_parse)
    found = _extract(
        "python",
        "from billing import charge as debit\ndef entry():\n    return debit()\n",
    )
    assert not any(item[1] == "call" for item in found)
