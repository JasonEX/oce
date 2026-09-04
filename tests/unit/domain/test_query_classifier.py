"""查询分类器测试 - 验证意图判定优先级"""

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
    """意图分类核心场景"""

    def test_symbol_with_extension_not_path(self):
        """Q33 回归：符号+扩展名应判为SYMBOL，不能误判为PATH"""
        query = "`invoke_handler` 在 lib.rs 中注册了哪些命令？"
        assert classify_query_intent(query) == QueryIntent.SYMBOL
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
        assert should_use_path_index(query)

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
        assert "ShouldBindJSON" in extract_code_identifiers(query)
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
