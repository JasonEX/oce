"""查询分类器测试 - 验证意图判定优先级"""

from oce.domain.services.query_classifier import (
    QueryIntent,
    classify_query_intent,
    should_use_path_index,
)
from oce.domain.services.query_symbols import extract_code_identifiers


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
    """意图分类核心场景"""

    def test_symbol_with_extension_not_path(self):
        """A registration question keeps semantic coverage and the exact operator."""
        query = "`invoke_handler` 在 lib.rs 中注册了哪些命令？"
        assert classify_query_intent(query) == QueryIntent.FEATURE
        assert not should_use_path_index(query)

    def test_symbol_location_queries(self):
        """Q31-Q40：符号定位类查询"""
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
        """Q41-Q50：调用链分析（符号+方向动词）"""
        queries = [
            "前端如何调用后端的 `add_provider` 命令？",
            "`auth_start_login` 的完整调用链：前端 → Tauri → Rust",
            "`copilot_get_models` 的调用路径是什么？",
            "`enable_prompt` 的调用链？",
        ]
        for q in queries:
            assert classify_query_intent(q) == QueryIntent.CALL_CHAIN

    def test_path_queries(self):
        """Q01-Q07：路径定位类（配置文件）"""
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
        """Q08-Q12：功能定位（无符号锚点）"""
        queries = [
            "MCP 服务器配置的管理逻辑在哪里？",
            "Provider 的增删改查操作在哪里实现？",
            "自动启动功能的实现代码在哪里？",
            "Session 使用统计的计算逻辑在哪里？",
        ]
        for q in queries:
            intent = classify_query_intent(q)
            # 这些查询可能判为 FEATURE 或 OVERVIEW，取决于是否含架构关键词
            assert intent in (QueryIntent.FEATURE, QueryIntent.OVERVIEW)

    def test_overview_queries(self):
        """Q24-Q26：架构理解类"""
        queries = [
            "系统托盘的实现和事件处理在哪里？",
            "应用初始化状态管理的实现在哪里？",
            "WebDAV 自动同步的调度逻辑在哪里？",
        ]
        for q in queries:
            intent = classify_query_intent(q)
            # 含"实现"+"事件处理"/"状态管理"/"调度"应判为 OVERVIEW
            assert intent == QueryIntent.OVERVIEW

    def test_reference_queries(self):
        """引用/使用类查询（符号+使用动词）"""
        queries = [
            "`tauri::command` 宏在哪些文件中使用？",
            "`get_providers` 在前端如何使用？",
            "`auth_poll_for_account` 如何被前端使用？",
        ]
        for q in queries:
            intent = classify_query_intent(q)
            # "如何使用" / "如何被使用" 应判为 REFERENCE
            assert intent in (QueryIntent.REFERENCE, QueryIntent.CALL_CHAIN)


class TestPathIndexRouting:
    """路径索引路由验证"""

    def test_should_use_path_index_delegates_to_intent(self):
        """should_use_path_index 应基于意图分类"""
        # PATH 意图 -> True
        assert should_use_path_index("Cargo.toml 在哪里？")

        # SYMBOL 意图 -> False（即使带扩展名）
        assert not should_use_path_index(
            "`invoke_handler` 在 lib.rs 中注册了哪些命令？"
        )

        # FEATURE 意图 -> False
        assert not should_use_path_index("如何实现自动重连功能？")

    def test_location_signal_does_not_force_focused_path_intent(self):
        query = "Where would you diagnose pending blobs that stopped progressing?"

        assert classify_query_intent(query) == QueryIntent.FEATURE
        assert not should_use_path_index(query)

    def test_type_location_is_a_symbol_query(self):
        query = "Where is WorkspaceContext defined?"

        assert classify_query_intent(query) == QueryIntent.SYMBOL
        assert not should_use_path_index(query)


class TestEdgeCases:
    """边界场景"""

    def test_no_symbol_with_extension_is_path(self):
        """无符号锚点 + 扩展名 + 路径关键词 -> PATH"""
        query = "主配置文件 config.json 在哪里？"
        assert classify_query_intent(query) == QueryIntent.PATH

    def test_symbol_without_extension_is_symbol(self):
        """符号锚点 + 无扩展名 -> SYMBOL"""
        query = "`add_provider` 函数在哪里定义？"
        assert classify_query_intent(query) == QueryIntent.SYMBOL

    def test_empty_query_defaults_to_feature(self):
        """空查询或纯问号默认 FEATURE"""
        assert classify_query_intent("这是什么项目？") == QueryIntent.FEATURE


class TestBilingualSymmetry:
    """中英对称性：英文查询应与中文得到同类意图，不因语言差异而误判"""

    def test_english_path_query_with_show_not_misclassified(self):
        """'show' 含子串 'how'，但按词边界不应触发功能标记，仍应判为 PATH"""
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
        """英文功能查询应偏向 FEATURE/OVERVIEW，而非找文件的 PATH"""
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
        """英文类型定位查询也应能抽出类型名做精确召回"""
        assert "Provider" in extract_code_identifiers(
            "Where is the Provider type defined?"
        )

    def test_english_type_keyword_not_substring_false_positive(self):
        """英文类型词按词边界匹配，不应从 'structure' 误抽出标识符"""
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
        assert extract_code_identifiers("see example.com and Foo.bar") == ("Foo.bar",)
        assert extract_code_identifiers("Session.request sends it") == (
            "Session.request",
        )

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


def test_mentions_alone_cannot_select_focused_recall():
    for query in (
        "Session.request sends it",
        "Explain how parseConfig handles invalid input",
    ):
        from oce.domain.services.query_classifier import route_query
        from oce.domain.services.query_evidence import extract_query_evidence

        route = route_query(query, extract_query_evidence(query))
        assert route.intent in (QueryIntent.FEATURE, QueryIntent.OVERVIEW)
        assert route.targets == ()


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


def test_definition_routing_survives_renaming_and_quoting():
    from oce.domain.services.query_classifier import route_query
    from oce.domain.services.query_evidence import extract_query_evidence

    for name in ("parseOptions", "collect_records", "Session.fetch", "acquire"):
        for spelling in (name, f"`{name}`"):
            for query in (
                f"Where is {spelling} defined?",
                f"Definition of {spelling}",
                f"找到 {spelling} 的定义",
                f"I am reviewing input handling.\nLocate the definition of {spelling}.",
            ):
                evidence = extract_query_evidence(query)
                route = route_query(query, evidence)
                assert evidence.identifiers == (name,), query
                assert route.intent == QueryIntent.SYMBOL, query
                assert route.targets == (name,), query


def test_overload_parameters_are_constraints_regardless_of_text_order():
    from oce.domain.services.query_classifier import route_query
    from oce.domain.services.query_evidence import extract_query_evidence

    for query in (
        "Definition of decodeRecord with ByteReader and DecodePolicy parameters",
        "找到接收 ByteReader 和 DecodePolicy 的 decodeRecord 重载定义",
    ):
        route = route_query(query, extract_query_evidence(query))
        assert route.intent == QueryIntent.SYMBOL
        assert route.targets == ("decodeRecord",)


def test_chinese_nominal_definitions_allow_optional_possessive():
    for name in ("TaxTable", "PolicyIndex"):
        for qualifier in ("类型", "类型的", "的", ""):
            assert (
                classify_query_intent(f"`{name}` {qualifier}定义") == QueryIntent.SYMBOL
            )


def test_call_chain_is_not_reclassified_when_an_intermediate_name_is_added():
    assert (
        classify_query_intent(
            "How does readPacket reach applyRecord through decodePacket?"
        )
        == QueryIntent.CALL_CHAIN
    )


def test_requests_for_tests_are_distinct_from_test_framework_behavior():
    from oce.domain.services.query_classifier import route_query
    from oce.domain.services.query_evidence import extract_query_evidence

    for query in ("Locate tests exercising parseOptions", "parseOptions 有测试吗？"):
        route = route_query(query, extract_query_evidence(query))
        assert route.intent == QueryIntent.REFERENCE and route.tests_requested
    route = route_query(
        "How does the test framework execute a function?",
        extract_query_evidence("How does the test framework execute a function?"),
    )
    assert not route.tests_requested


def test_a_named_intermediate_does_not_anchor_an_unnamed_chain_start():
    from oce.domain.services.query_classifier import route_query
    from oce.domain.services.query_evidence import extract_query_evidence

    for name in ("IntoResponse", "PacketAdapter"):
        query = f"Trace a request from connection acceptance through dispatch and {name} conversion."
        route = route_query(query, extract_query_evidence(query))
        assert route.intent == QueryIntent.CALL_CHAIN
        assert route.targets == ()


def test_incidental_definition_nouns_do_not_make_an_explanation_focused():
    for query in (
        "How does makeReducer turn case definitions into actions?",
        "How does a RecordAdapter use its RecordFactory list?",
    ):
        assert classify_query_intent(query) != QueryIntent.SYMBOL
    assert (
        classify_query_intent(
            "Trace how buildService initializes modules and injects endpoint definitions."
        )
        == QueryIntent.CALL_CHAIN
    )


def test_chain_endpoints_follow_direction_instead_of_mention_count():
    from oce.domain.services.query_classifier import route_query
    from oce.domain.services.query_evidence import extract_query_evidence

    for query in (
        "Trace readPacket through decodePacket to applyRecord.",
        "readPacket 如何通过 decodePacket 到达 applyRecord？",
        "How does readPacket reach applyRecord using DecodePolicy?",
    ):
        route = route_query(query, extract_query_evidence(query))
        assert route.intent == QueryIntent.CALL_CHAIN
        assert route.targets == ("readPacket", "applyRecord")
