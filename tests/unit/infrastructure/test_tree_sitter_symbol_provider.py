"""tree-sitter definitions and imports across grammars, with regex fallback."""

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
