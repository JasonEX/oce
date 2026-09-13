"""Query classifier tests: the intent priority rules."""

from oce.domain.services.query_classifier import (
    QueryIntent,
    classify_query_intent,
    extract_code_identifiers,
    should_use_path_index,
)


def test_extract_code_identifiers_preserves_explicit_anchors():
    assert extract_code_identifiers("`copilot_get_models` 的调用路径是什么？") == (
        "copilot_get_models",
    )


def test_extract_code_identifiers_supports_type_location_queries():
    assert extract_code_identifiers("Provider 的前后端类型定义在哪里？") == (
        "Provider",
    )


def test_extract_code_identifiers_supports_uppercase_constants():
    assert extract_code_identifiers("OCE_WORKSPACE 和 OCE_API_URL 在哪里解析？") == (
        "OCE_WORKSPACE",
        "OCE_API_URL",
    )


def test_extract_code_identifiers_ignores_product_names():
    assert extract_code_identifiers("如何调用 Tauri 的文件对话框？") == ()


class TestQueryIntentClassification:
    """Core classification cases."""

    def test_symbol_with_extension_not_path(self):
        """Regression: a symbol plus a file extension is SYMBOL, not PATH."""
        query = "`invoke_handler` 在 lib.rs 中注册了哪些命令？"
        assert classify_query_intent(query) == QueryIntent.SYMBOL
        assert not should_use_path_index(query)

    def test_symbol_location_queries(self):
        """Symbol location requests."""
        queries = [
            "`add_provider` 函数在哪个 Rust 文件定义？",
            "`get_providers` 函数的实现位置？",
            "`auth_start_login` 函数在哪里定义？",
            "`copilot_get_models` 的实现文件是？",
        ]
        for q in queries:
            assert classify_query_intent(q) == QueryIntent.SYMBOL
            assert not should_use_path_index(q)

    def test_call_chain_queries(self):
        """Call-chain requests: a symbol plus a directional verb."""
        queries = [
            "前端如何调用后端的 `add_provider` 命令？",
            "`auth_start_login` 的完整调用链：前端 → Tauri → Rust",
            "`copilot_get_models` 的调用路径是什么？",
            "`enable_prompt` 的调用链？",
        ]
        for q in queries:
            assert classify_query_intent(q) == QueryIntent.CALL_CHAIN

    def test_path_queries(self):
        """Path requests (configuration files)."""
        queries = [
            "Cargo 依赖配置文件在哪里？",
            "Node.js 的 package.json 在哪里？",
            "TypeScript 的配置文件在哪里？",
            "Tauri 的主窗口配置在哪里？",
            "i18n 的中文翻译文件在哪里？",
        ]
        for q in queries:
            assert classify_query_intent(q) == QueryIntent.PATH
            assert should_use_path_index(q)

    def test_feature_queries(self):
        """Feature requests without a symbol anchor."""
        queries = [
            "MCP 服务器配置的管理逻辑在哪里？",
            "Provider 的增删改查操作在哪里实现？",
            "自动启动功能的实现代码在哪里？",
            "Session 使用统计的计算逻辑在哪里？",
        ]
        for q in queries:
            intent = classify_query_intent(q)
            # FEATURE or OVERVIEW depending on architecture keywords.
            assert intent in (QueryIntent.FEATURE, QueryIntent.OVERVIEW)

    def test_overview_queries(self):
        """Architecture requests."""
        queries = [
            "系统托盘的实现和事件处理在哪里？",
            "应用初始化状态管理的实现在哪里？",
            "WebDAV 自动同步的调度逻辑在哪里？",
        ]
        for q in queries:
            intent = classify_query_intent(q)
            # Implementation plus event handling, state management or scheduling is OVERVIEW.
            assert intent == QueryIntent.OVERVIEW

    def test_reference_queries(self):
        """Reference requests: a symbol plus a use verb."""
        queries = [
            "`tauri::command` 宏在哪些文件中使用？",
            "`get_providers` 在前端如何使用？",
            "`auth_poll_for_account` 如何被前端使用？",
        ]
        for q in queries:
            intent = classify_query_intent(q)
            # "how is it used" is REFERENCE
            assert intent in (QueryIntent.REFERENCE, QueryIntent.CALL_CHAIN)


class TestPathIndexRouting:
    """Path index routing."""

    def test_should_use_path_index_delegates_to_intent(self):
        """should_use_path_index follows the intent."""
        # PATH intent
        assert should_use_path_index("Cargo.toml 在哪里？")

        # SYMBOL intent, even with a file extension
        assert not should_use_path_index(
            "`invoke_handler` 在 lib.rs 中注册了哪些命令？"
        )

        # FEATURE intent
        assert not should_use_path_index("如何实现自动重连功能？")

    def test_location_signal_does_not_force_focused_path_intent(self):
        query = "Where would you diagnose pending blobs that stopped progressing?"

        assert classify_query_intent(query) == QueryIntent.FEATURE
        assert should_use_path_index(query)

    def test_type_location_is_a_symbol_query(self):
        query = "Where is WorkspaceContext defined?"

        assert classify_query_intent(query) == QueryIntent.SYMBOL
        assert not should_use_path_index(query)


class TestEdgeCases:
    """Edge cases."""

    def test_no_symbol_with_extension_is_path(self):
        """No symbol anchor, a file extension and a path word: PATH."""
        query = "主配置文件 config.json 在哪里？"
        assert classify_query_intent(query) == QueryIntent.PATH

    def test_symbol_without_extension_is_symbol(self):
        """A symbol anchor without a file extension: SYMBOL."""
        query = "`add_provider` 函数在哪里定义？"
        assert classify_query_intent(query) == QueryIntent.SYMBOL

    def test_empty_query_defaults_to_feature(self):
        """A bare question defaults to FEATURE."""
        assert classify_query_intent("这是什么项目？") == QueryIntent.FEATURE


class TestBilingualSymmetry:
    """English requests get the same intents as their Chinese counterparts."""

    def test_english_path_query_with_show_not_misclassified(self):
        """'show' contains 'how' but must not trigger the feature marker; still PATH."""
        query = "show me where the config file is"
        assert classify_query_intent(query) == QueryIntent.PATH
        assert should_use_path_index(query)

    def test_english_filename_query_is_path(self):
        query = "where is the tsconfig.json file?"
        assert classify_query_intent(query) == QueryIntent.PATH
        assert should_use_path_index(query)

    def test_snake_case_path_is_not_a_symbol(self):
        query = "Where is the src/pylint/message/message_definition.py file?"

        assert extract_code_identifiers(query) == ()
        assert classify_query_intent(query) == QueryIntent.PATH
        assert should_use_path_index(query)

    def test_dunder_filename_is_not_a_symbol(self):
        query = "Where is the src/_pytest/config/__init__.py file?"

        assert extract_code_identifiers(query) == ()
        assert classify_query_intent(query) == QueryIntent.PATH
        assert should_use_path_index(query)

    def test_symbol_outside_a_path_remains_a_symbol(self):
        query = "Where is load_config defined in src/config_loader.py?"

        assert extract_code_identifiers(query) == ("load_config",)
        assert classify_query_intent(query) == QueryIntent.SYMBOL

    def test_backticked_symbol_remains_primary_over_a_path(self):
        query = "Where is `load_config` defined in src/config_loader.py?"

        assert extract_code_identifiers(query) == ("load_config",)
        assert classify_query_intent(query) == QueryIntent.SYMBOL

    def test_english_feature_query_not_path(self):
        """English feature requests lean to FEATURE/OVERVIEW rather than PATH."""
        query = "where is the retry logic implemented?"
        assert classify_query_intent(query) in (
            QueryIntent.FEATURE,
            QueryIntent.OVERVIEW,
        )
        assert not should_use_path_index(query)

    def test_english_call_chain_query(self):
        query = "how is `add_provider` called from the frontend?"
        assert classify_query_intent(query) == QueryIntent.CALL_CHAIN

    def test_english_call_verb_requires_a_complete_token(self):
        query = "Where is CallbackRegistry class defined?"
        assert classify_query_intent(query) == QueryIntent.SYMBOL

    def test_two_explicit_facets_are_compound(self):
        query = "Locate `load_config`. Explain how startup validates it."
        assert classify_query_intent(query) == QueryIntent.COMPOUND

    def test_multiple_facets_without_a_symbol_are_compound(self):
        query = "Explain authentication behavior. Describe the retry policy."
        assert classify_query_intent(query) == QueryIntent.COMPOUND

    def test_english_reference_query(self):
        query = "how is `get_providers` used in the frontend?"
        assert classify_query_intent(query) in (
            QueryIntent.REFERENCE,
            QueryIntent.CALL_CHAIN,
        )

    def test_english_referenced_query(self):
        query = "Where is `get_providers` referenced?"
        assert classify_query_intent(query) == QueryIntent.REFERENCE

    def test_english_overview_query(self):
        query = "the state management architecture of the app"
        assert classify_query_intent(query) == QueryIntent.OVERVIEW

    def test_english_type_identifier_extraction(self):
        """English type requests yield the type name for exact recall."""
        assert "Provider" in extract_code_identifiers(
            "Where is the Provider type defined?"
        )

    def test_english_type_keyword_not_substring_false_positive(self):
        """Type words match at word boundaries; 'structure' yields no identifier."""
        assert extract_code_identifiers("Explain the Data structure here") == ()


class TestDottedQualifiedNames:
    def test_dotted_camel_case_is_a_symbol_not_a_filename(self):
        query = (
            "Trace gin's JSON request binding from Context.ShouldBindJSON through "
            "the binding package's JSON binding into struct validation."
        )
        # The qualified spelling is kept whole; the pipeline derives the leaf.
        assert extract_code_identifiers(query) == ("Context.ShouldBindJSON",)
        assert classify_query_intent(query) == QueryIntent.CALL_CHAIN

    def test_plain_dotted_words_and_domains_are_not_symbols(self):
        assert extract_code_identifiers("see example.com and Foo.bar") == ()
        assert extract_code_identifiers("Session.request sends it") == ()

    def test_real_filenames_still_route_to_path(self):
        assert classify_query_intent("where is the tsconfig.json file?") == (
            QueryIntent.PATH
        )
        assert classify_query_intent("lib.rs 在哪里") == QueryIntent.PATH

    def test_long_engineering_extensions_are_files_not_qualified_names(self):
        for query in (
            "where is build.csproj?",
            "where is settings.gradle?",
            "where is application.properties?",
        ):
            assert classify_query_intent(query) == QueryIntent.PATH


def test_reviewed_semantic_queries_use_their_declared_routing_intent():
    from benchmarks.blackbox.semantic_queries import DEFAULT_CASES, load_manifest

    assert {
        case.id: classify_query_intent(case.query).value
        for case in load_manifest(DEFAULT_CASES)
        if classify_query_intent(case.query).value != case.kind
    } == {}


def test_test_and_implementor_questions_ask_for_use_sites():
    from oce.domain.services.query_classifier import QueryIntent, classify_query_intent

    assert classify_query_intent("Which tests cover `approx`?") == QueryIntent.REFERENCE
    assert classify_query_intent("哪些测试覆盖了 `approx`？") == QueryIntent.REFERENCE
    assert (
        classify_query_intent("Which classes implement `TypeAdapterFactory`?")
        == QueryIntent.REFERENCE
    )
    # A plain "how is it implemented" question still asks for the definition.
    assert classify_query_intent("How is `approx` implemented?") == QueryIntent.SYMBOL


class TestCallersDefinitionsAndEndpoints:
    def test_callers_of_one_symbol_are_a_reference_question(self):
        for query in (
            "Which functions call `extract_cookies_to_jar`?",
            "Where is `_is_ignored_file` called?",
            "Which code calls `app.render`?",
            "哪些地方调用了 `get_debug_flag`？",
            "谁调用了 `get_debug_flag`？",
        ):
            assert classify_query_intent(query) == QueryIntent.REFERENCE, query

    def test_flow_questions_stay_call_chains(self):
        for query in (
            "how is `add_provider` called from the frontend?",
            "前端如何调用后端的 `add_provider` 命令？",
            "`enable_prompt` 的调用链？",
            "Trace how `Flask.wsgi_app` dispatches a request to the view function.",
        ):
            assert classify_query_intent(query) == QueryIntent.CALL_CHAIN, query

    def test_two_endpoints_in_a_how_question_are_a_call_chain(self):
        for query in (
            "How does `requests.get` reach `HTTPAdapter.send`?",
            "How does constructing a `DataArray` reach `as_compatible_data`?",
            "`Engine.ServeHTTP` 如何到达 `handleHTTPRequest`？",
        ):
            assert classify_query_intent(query) == QueryIntent.CALL_CHAIN, query
        # Two symbols without a flow question remain two facets, except an
        # impl lookup, which asks for the implementing use site.
        assert (
            classify_query_intent(
                "Where is `IntoResponse` implemented for `StatusCode`?"
            )
            == QueryIntent.REFERENCE
        )
        assert (
            classify_query_intent("Compare `Session.get` with `requests.get`.")
            == QueryIntent.COMPOUND
        )

    def test_definition_question_with_parameter_types_is_a_symbol_question(self):
        query = (
            "Where is the `fromJson` overload taking a `JsonReader` "
            "and a `TypeToken` defined?"
        )
        assert extract_code_identifiers(query) == (
            "fromJson",
            "JsonReader",
            "TypeToken",
        )
        assert classify_query_intent(query) == QueryIntent.SYMBOL
        assert classify_query_intent("`Session.get` 和 `Cache` 在哪里定义？") == (
            QueryIntent.SYMBOL
        )

    def test_private_names_are_extracted_once(self):
        assert extract_code_identifiers("Where is `_is_ignored_file` called?") == (
            "_is_ignored_file",
        )
        assert extract_code_identifiers("Where is `_infer_coords_and_dims` used?") == (
            "_infer_coords_and_dims",
        )


def test_dunder_and_private_names_are_extracted_whole():
    # ``__init__`` used to leak a fragment (``init__``) that turned a two-symbol
    # "how does A reach B" question into a compound one; private names keep
    # their underscores because that is how the index spells them.
    assert extract_code_identifiers(
        "How does `DataArray.__init__` reach `as_compatible_data`?"
    ) == ("DataArray.__init__", "as_compatible_data")
    assert (
        classify_query_intent(
            "How does `DataArray.__init__` reach `as_compatible_data`?"
        )
        == QueryIntent.CALL_CHAIN
    )
    assert extract_code_identifiers("Where is _is_ignored_file used?") == (
        "_is_ignored_file",
    )


def test_private_and_public_spellings_remain_distinct_identifiers():
    query = "How does `_start_flow` reach `start_flow`?"

    assert extract_code_identifiers(query) == ("_start_flow", "start_flow")
    assert classify_query_intent(query) == QueryIntent.CALL_CHAIN
