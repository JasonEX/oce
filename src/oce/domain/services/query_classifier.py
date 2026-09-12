"""查询分类器 - 按意图分类，支持策略派发"""

from __future__ import annotations

import re
from enum import StrEnum

from oce.domain.chunk.lang import detect_language
from oce.domain.services.query_planner import HeuristicQueryPlanner

# ──────────────────────────────────────────────────────────────────────────────
# 意图枚举
# ──────────────────────────────────────────────────────────────────────────────


class QueryIntent(StrEnum):
    """查询意图类型，用于派发检索策略"""

    SYMBOL = "symbol"  # 符号定位：某函数/类型在哪里定义
    CALL_CHAIN = "call_chain"  # 调用链分析：前端如何调用某后端命令
    REFERENCE = "reference"  # 引用分析：某符号在别处如何被使用
    PATH = "path"  # 路径定位：某配置文件在哪里
    FEATURE = "feature"  # 功能定位：某功能的实现在哪里
    OVERVIEW = "overview"  # 架构理解：某子系统的实现与事件处理
    COMPOUND = "compound"  # 复合查询：多 facet 或并列条件


# ──────────────────────────────────────────────────────────────────────────────
# 特征模式
# ──────────────────────────────────────────────────────────────────────────────

_IDENTIFIER_PATTERN = re.compile(
    r"^[A-Za-z_$][A-Za-z0-9_$]*(?:::[A-Za-z_$][A-Za-z0-9_$]*)*$"
)
# Whole identifiers only: ``__init__`` must not yield a fragment such as
# ``init__``, and private names keep their leading underscores as spelled.
_SNAKE_IDENTIFIER_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])_*[a-z][a-z0-9]*_[a-z0-9_]+(?![A-Za-z0-9_])"
)
_QUALIFIED_IDENTIFIER_PATTERN = re.compile(
    r"[A-Za-z_$][A-Za-z0-9_$]*(?:::[A-Za-z_$][A-Za-z0-9_$]*)+"
)
_TYPE_IDENTIFIER_PATTERN = re.compile(
    r"([A-Z][A-Za-z0-9_$]*)\s*(?:的)?(?:前后端)?"
    r"(?:类型|类|接口|结构|定义|"
    r"(?:type|interface|struct|enum|trait|class|definition|defined|implemented)\b)"
)
_CONSTANT_IDENTIFIER_PATTERN = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")

# 带扩展名的文件名 token（如 config.json / lib.rs）：定位具体文件的强结构信号。
# 扩展名首位限定为字母，避免把版本号 3.13 之类误判为文件名。
# 先找带扩展名的 token，再用已支持语言/常见工程扩展名过滤。仅靠长度
# 会丢掉 ``build.csproj`` 和 ``application.properties``，而不过滤又会把
# ``Session.request`` 当文件。
_FILENAME_TOKEN_PATTERN = re.compile(
    r"[A-Za-z0-9_\-]+\.[A-Za-z][A-Za-z0-9]{0,15}(?![A-Za-z0-9_])"
)
_EXTRA_FILE_SUFFIXES = frozenset(
    {
        ".adoc",
        ".cfg",
        ".csv",
        ".csproj",
        ".env",
        ".fsproj",
        ".gradle",
        ".ini",
        ".lock",
        ".properties",
        ".props",
        ".proto",
        ".rst",
        ".sln",
        ".targets",
        ".tf",
        ".txt",
        ".vbproj",
    }
)


def _is_probable_filename(token: str) -> bool:
    suffix = "." + token.rsplit(".", 1)[-1].lower()
    return detect_language(token) is not None or suffix in _EXTRA_FILE_SUFFIXES


def _has_filename(text: str) -> bool:
    return any(
        _is_probable_filename(match.group())
        for match in _FILENAME_TOKEN_PATTERN.finditer(text)
    )


def _mask_filenames(text: str) -> str:
    return _FILENAME_TOKEN_PATTERN.sub(
        lambda match: " " if _is_probable_filename(match.group()) else match.group(),
        text,
    )


# 点号限定名（``Context.ShouldBindJSON``、``requests.Session.request``）：末段是
# CamelCase 或 snake_case 时才是代码符号；``example.com``、``Foo.bar`` 不算。
_DOTTED_IDENTIFIER_PATTERN = re.compile(
    r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*"
    r"\.((?:[A-Z][a-z0-9]+(?:[A-Z][A-Za-z0-9]*)+)|(?:[a-z]+_[a-z0-9_]+)|(?:[A-Z][A-Z0-9]+[a-z][A-Za-z0-9]*))\b"
)
# 完整路径先于 snake_case 标识符解析；否则 ``src/message_definition.py``
# 会伪造出 ``message_definition`` 符号，``__init__.py`` 也会伪造出 ``init__``。
_PATH_TOKEN_PATTERN = re.compile(r"(?:[A-Za-z0-9_.\-]+[/\\])+[A-Za-z0-9_.\-]+")

# 调用链动词（跨边界/路径导向）。英文只保留真正表达调用关系的动词：
# to / from / path 在 issue 文本里几乎必然出现，曾让几乎所有英文长查询都判成调用链。
_CALL_VERBS = {
    "调用",
    "触发",
    "执行",
    "从",
    "到",
    "路径",
    "流程",
    "完整",
    "如何被",
    "如何从",
    "追踪",
    "call",
    "called",
    "calling",
    "calls",
    "trace",
    "traced",
    "traces",
    "tracing",
    "invoke",
    "invoked",
    "invokes",
    "invoking",
    "trigger",
    "triggered",
    "triggering",
    "triggers",
    "execute",
    "executed",
    "executes",
    "executing",
    "flow",
    "flows",
}

# 名词式路径词（file/config/依赖）只在这个长度以内的问句里当作找文件的信号。
_PATH_KEYWORD_MAX_CHARS = 100
# 带符号的长句里出现架构词时按概览处理的最小词数。
_OVERVIEW_MIN_WORDS = 8

# issue 风格的长文本：多个标识符或 planner 能切出多个明确 facet。
# 它描述的是复合问题，不能因为其中某个动词就按单一符号的调用链或引用来路由。
_COMPOUND_IDENTIFIER_LIMIT = 2
_COMPOUND_PLANNER = HeuristicQueryPlanner(max_queries=3)

# 引用/使用动词（单向依赖）
_REFERENCE_VERBS = {
    "使用",
    "引用",
    "导入",
    "依赖",
    "消费",
    "接收",
    "use",
    "reference",
    "import",
    "depend",
    "consume",
    "receive",
}

# 概览/架构关键词
_OVERVIEW_KEYWORDS = {
    "架构",
    "实现",
    "事件处理",
    "状态管理",
    "调度",
    "机制",
    "流程",
    "解释",
    "生命周期",
    "哪些模块",
    "architecture",
    "explain",
    "lifecycle",
    "which modules",
    "modules own",
    "responsible for",
    "implementation",
    "event handling",
    "state management",
    "scheduling",
    "mechanism",
    "workflow",
}

_EXPLICIT_OVERVIEW_CUES = {
    "架构",
    "生命周期",
    "architecture",
    "overview",
    "lifecycle",
    "event handling",
    "state management",
}
_TRACE_QUERY = re.compile(r"(?i)^\s*(?:trace|tracing)\b|^\s*(?:追踪|跟踪)")

# 通用路径定位词（指向“文件/配置”实体，对任意仓库成立）
_PATH_KEYWORDS = {
    "文件",
    "配置",
    "在哪里",
    "在哪",
    "哪个文件",
    "翻译文件",
    "依赖",
    "file",
    "config",
    "where",
    "location",
    "dependency",
}

# 决定 focused PATH 意图的强信号。普通 "where/在哪里" 只说明用户想定位代码，
# 仍可能是跨文件功能问题；它可以启用 path operator，但不应强制 focused selection。
_EXPLICIT_PATH_KEYWORDS = {
    "文件",
    "哪个文件",
    "路径",
    "配置",
    "依赖",
    "file",
    "files",
    "path",
    "paths",
    "config",
    "configuration",
    "dependency",
    "dependencies",
}

# 功能/实现类查询标记：出现这些词时，即便含“文件/配置/在哪里”也偏向功能定位而非找文件
_FEATURE_MARKERS = {
    "功能",
    "实现",
    "逻辑",
    "代码",
    "机制",
    "策略",
    "如何",
    "怎样",
    "怎么",
    "feature",
    "implement",
    "implementation",
    "logic",
    "code",
    "mechanism",
    "strategy",
    "behavior",
    "how",
}


def _terms_pattern(
    terms: set[str],
    *,
    match_ascii_prefix: bool = True,
) -> re.Pattern[str]:
    """把关键词集合编译成判定正则。

    英文（ASCII）词用前缀词边界匹配：既避免子串误命中（how 命中 show、file 命中
    profile），又能覆盖词形变化（implement→implemented、config→configuration）。
    中文无词边界概念，按子串匹配。目的是让中英查询判定对称，不偏向任一语言。
    """
    parts = []
    for term in terms:
        if not term.isascii():
            parts.append(re.escape(term))
        elif match_ascii_prefix:
            parts.append(rf"\b{re.escape(term)}")
        else:
            parts.append(rf"\b{re.escape(term)}\b")
    return re.compile("|".join(parts))


# 调用词必须是完整 token；显式列出常见词形，避免 ``call`` 误命中
# ``callback`` 或 ``execute`` 误命中 ``executor``。
_CALL_VERBS_RE = _terms_pattern(_CALL_VERBS, match_ascii_prefix=False)
_REFERENCE_VERBS_RE = _terms_pattern(_REFERENCE_VERBS)
_OVERVIEW_KEYWORDS_RE = _terms_pattern(_OVERVIEW_KEYWORDS)
_EXPLICIT_OVERVIEW_CUES_RE = _terms_pattern(_EXPLICIT_OVERVIEW_CUES)
_PATH_KEYWORDS_RE = _terms_pattern(_PATH_KEYWORDS)
_EXPLICIT_PATH_KEYWORDS_RE = _terms_pattern(
    _EXPLICIT_PATH_KEYWORDS,
    match_ascii_prefix=False,
)
_FEATURE_MARKERS_RE = _terms_pattern(_FEATURE_MARKERS)


# Only requests that ask *for* tests. "How does bats run a test function" is
# about the framework's source, not about finding tests.
_TEST_QUERY = re.compile(
    r"(?i)\b(?:which|what|find|show|where\s+(?:is|are))\b[^.?\n]{0,60}\btests?\b"
    r"|\btests?\s+(?:for|of|covering|that\s+cover)\b"
    r"|\b(?:unit|integration|regression)\s+tests?\b"
    r"|\btest\s*cases?\b|\bconftest\b|\bfixtures?\s+for\b"
    r"|测试用例|单元测试|哪个测试|测试在哪|有没有测试|相关测试|哪些测试"
)
_TEST_QUERY_MAX_CHARS = 200
# "Which classes implement X" asks for the subtypes, i.e. the places that use
# the name in an ``extends``/``implements`` position, not for X's declaration.
# "Where is ``IntoResponse`` implemented for ``StatusCode``" asks for the impl
# block, i.e. the place that uses the trait name in an implementing position.
_IMPLEMENTORS_QUERY = re.compile(
    r"(?i)\b(?:which|what|all|list|every)\b[^.?\n]{0,40}"
    r"\b(?:implement|extend|subclass|override|inherit|derive)"
    r"|\bimplemented\s+(?:for|by|on)\b"
    r"|哪些.{0,12}(?:实现|继承|重写|派生|子类)"
)


# "Which functions call X" / "哪些地方调用了 X" / "Where is X called?" ask for the
# callers of one symbol: its use sites, a reference question. A call-chain
# question describes a flow ("how does A reach B", "trace", "调用链") instead.
_CALLERS_QUERY = re.compile(
    r"(?i)\b(?:which|what|who|where|all|list|every)\b[^.?\n]{0,40}"
    r"\b(?:calls?|called|calling|invokes?|invoked|invoking|triggers?|triggered)\b"
    r"|哪些.{0,12}(?:调用|触发|执行)|谁.{0,6}调用|被.{0,8}调用"
)
# "Where is X defined?" is a definition question however many type names it
# spells out to pick an overload; the extra names are disambiguation, not
# further facets.
_DEFINITION_QUERY = re.compile(
    r"(?i)\b(?:where|which\s+file)\b[^.?\n]{0,80}\bdefined\b"
    r"|在哪里定义|定义在哪|在哪个文件定义"
)
# A "how" question that names two symbols asks for the path between them.
_HOW_QUERY = re.compile(r"(?i)\bhow\b|如何|怎样|怎么")
_QUESTION_MAX_CHARS = 200


# A request that asks (how, why, which, explain, trace) rather than states.
# A docstring-shaped description ("Fetches the securities that match the
# filters") names one function; a question about how something works names
# the subsystem whose entry points answer it.
_ASKS_HOW = re.compile(
    r"(?i)\b(?:how|why|which|what|where|explain|describe|trace|walk\s+through)\b"
    r"|如何|怎么|怎样|为什么|解释|说明|哪些|哪个|什么"
)


def asks_how(query: str) -> bool:
    return _ASKS_HOW.search(query) is not None


def asks_for_callers(query: str) -> bool:
    return (
        len(query) <= _QUESTION_MAX_CHARS and _CALLERS_QUERY.search(query) is not None
    )


def asks_for_definition(query: str) -> bool:
    return (
        len(query) <= _QUESTION_MAX_CHARS
        and _DEFINITION_QUERY.search(query) is not None
    )


def asks_about_tests(query: str) -> bool:
    """A question-sized request that names tests wants test files as the answer.

    Long issue-style text mentions failing tests while asking about the code
    under test, so the rule is limited to question-sized requests.
    """
    return len(query) <= _TEST_QUERY_MAX_CHARS and _TEST_QUERY.search(query) is not None


def asks_for_implementors(query: str) -> bool:
    return _IMPLEMENTORS_QUERY.search(query) is not None


# ──────────────────────────────────────────────────────────────────────────────
# 主分类函数
# ──────────────────────────────────────────────────────────────────────────────


def classify_query_intent(query: str) -> QueryIntent:
    """
    按意图分类查询，用于派发检索策略。

    判定优先级（从高到低）：
    1. 显式询问已命名符号的定义 → SYMBOL
    2. 标识符超过 2 个或 planner 切出至少 3 个 facet → COMPOUND
    3. 显式 trace → CALL_CHAIN；显式架构/生命周期 → OVERVIEW
    4. 单符号调用方问题 → REFERENCE；其他调用类动词 → CALL_CHAIN
    5. 有符号锚点（反引号/snake_case/::/限定名）：
       - 引用、测试或实现者问题 → REFERENCE
       - 两端点 how 问题 → CALL_CHAIN；其他多标识符问题 → COMPOUND
       - 其余 → SYMBOL
    6. 无符号锚点：
       - 概览词 → OVERVIEW
       - 已知文件名或短问句中的文件/配置词（非功能类）→ PATH
       - 其余 → FEATURE

    Examples:
        >>> classify_query_intent("`parse_config` 函数在哪里定义？")
        QueryIntent.SYMBOL

        >>> classify_query_intent("前端如何调用后端的 `parse_config`？")
        QueryIntent.CALL_CHAIN

        >>> classify_query_intent("config.json 在哪里？")
        QueryIntent.PATH

        >>> classify_query_intent("`parse_config` 在 server.py 中注册了哪些路由？")
        QueryIntent.SYMBOL  # 符号优先，不因扩展名改判为 PATH
    """
    query_lower = query.lower()
    identifiers = extract_code_identifiers(query)
    has_symbol = bool(identifiers)

    # 提取反引号、路径和文件名之外的文本，避免符号名或路径片段被动词误匹配：
    # `invoke_handler` 中的 invoke、src/execute.c 中的 execute 都不是调用链动词。
    text_outside_backticks = re.sub(r"`[^`]+`", "", query_lower)
    text_outside_backticks = _PATH_TOKEN_PATTERN.sub(" ", text_outside_backticks)
    text_outside_backticks = _mask_filenames(text_outside_backticks)

    # 「X 在哪里定义」点名再多参数类型也是一个定义问题：``fromJson`` 的重载靠
    # ``JsonReader``/``TypeToken`` 消歧，这些名字不是新的 facet。
    if has_symbol and asks_for_definition(text_outside_backticks):
        return QueryIntent.SYMBOL

    # 多 facet 是查询本身的广度信号，不依赖是否能从自然语言中提取出代码符号。
    # 放在符号分支外，避免无显式标识符的 issue 被一个 file/config 词缩成 PATH。
    if (
        len(identifiers) > _COMPOUND_IDENTIFIER_LIMIT
        or len(_COMPOUND_PLANNER.plan(query)) >= 3
    ):
        return QueryIntent.COMPOUND

    word_count = len(query.split())

    # ``Trace ...`` 是最强的调用链证据，即使后文提到 lifecycle 也不改变意图。
    if _TRACE_QUERY.search(text_outside_backticks):
        return QueryIntent.CALL_CHAIN

    # 显式架构/生命周期问题可以包含 execute/flow 等过程动词，但仍在问
    # 系统覆盖面，不是追踪一条调用边。
    if _EXPLICIT_OVERVIEW_CUES_RE.search(text_outside_backticks):
        return QueryIntent.OVERVIEW

    # 「哪些地方调用了 X」问的是一个符号的使用位置，不是一条调用链。
    if len(identifiers) == 1 and asks_for_callers(text_outside_backticks):
        return QueryIntent.REFERENCE

    # 调用链特征：方向性动词（trace/call/flow…）。``Trace requests.request through
    # Session.send`` 里的限定名不一定能抽成符号，动词本身已经说明了问题形态。
    if _CALL_VERBS_RE.search(text_outside_backticks):
        return QueryIntent.CALL_CHAIN

    # 分支1：有符号锚点
    if has_symbol:
        # 引用分析：使用/依赖类动词 + 符号（动词在反引号外）
        if _REFERENCE_VERBS_RE.search(text_outside_backticks):
            return QueryIntent.REFERENCE
        # "Which tests cover X" and "which classes implement X" ask for the
        # places that exercise or extend the symbol: use sites, not its
        # declaration.
        if asks_about_tests(query) or asks_for_implementors(text_outside_backticks):
            return QueryIntent.REFERENCE

        # 「A 如何到达 B」：两个端点之间的路径是一条调用链，不是两个并列问题。
        if (
            len(identifiers) == 2
            and len(query) <= _QUESTION_MAX_CHARS
            and _HOW_QUERY.search(text_outside_backticks)
        ):
            return QueryIntent.CALL_CHAIN

        if len(identifiers) > 1:
            return QueryIntent.COMPOUND

        # 长句里的架构描述顺带提到一个符号（``Engine.ServeHTTP``）仍是概览，
        # 短问句里的符号才是定位目标。
        if word_count >= _OVERVIEW_MIN_WORDS and _OVERVIEW_KEYWORDS_RE.search(
            text_outside_backticks
        ):
            return QueryIntent.OVERVIEW

        # 默认符号定位
        return QueryIntent.SYMBOL

    # 分支2：无符号锚点。用结构信号（文件名 token / 通用路径词）判定，不枚举技术栈。
    has_feature_marker = bool(_FEATURE_MARKERS_RE.search(query_lower))

    # 概览类：架构/机制/流程描述。先于路径判定：``Explain the Gson architecture:
    # ... configuration ...`` 里的 configuration 是名词，不是在找配置文件。
    if _OVERVIEW_KEYWORDS_RE.search(query_lower):
        return QueryIntent.OVERVIEW

    # 显式文件名决定 focused PATH 意图；文件/路径/配置这类名词只在短问句里
    # 才是找文件的信号，长句里它们只是描述的一部分。
    if not has_feature_marker and (
        _has_filename(query)
        or (
            len(query) <= _PATH_KEYWORD_MAX_CHARS
            and _EXPLICIT_PATH_KEYWORDS_RE.search(query_lower)
        )
    ):
        return QueryIntent.PATH

    # 默认功能定位
    return QueryIntent.FEATURE


# ``Session.get`` / ``binding.Default``: identifier segments joined by dots. The
# qualifier is kept because it disambiguates same-named declarations; the
# retrieval pipeline derives the leaf to look up.
_DOTTED_QUALIFIED_PATTERN = re.compile(
    r"^[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)+$"
)


def extract_code_identifiers(query: str) -> tuple[str, ...]:
    """提取适合精确词法召回的代码标识符，保持查询中的出现顺序。

    限定名（``Session.get``、``a::b::C``）整体保留一个标识符：限定词是消歧证据，
    叶子名由检索管线派生，不在这里拆开，否则一个限定名会被算成两个符号。
    """
    identifiers: list[str] = []

    def add(value: str) -> None:
        value = value.strip()
        if not (
            _IDENTIFIER_PATTERN.fullmatch(value)
            or _DOTTED_QUALIFIED_PATTERN.fullmatch(value)
        ):
            return
        # The leaf of a qualified name already listed is the same symbol.
        # Leading underscores are significant: ``_load_config`` and
        # ``load_config`` may both exist in the same scope.
        if value in identifiers or any(
            item.endswith((f".{value}", f"::{value}")) for item in identifiers
        ):
            return
        identifiers.append(value)

    for value in re.findall(r"`([^`]+)`", query):
        add(value)

    # 反引号依旧从原文提取；启发式扫描则排除路径和文件名，避免把文件命名
    # 误当成 exact-symbol 证据。路径外的 ``load_config`` 等标识符不受影响。
    identifier_text = _PATH_TOKEN_PATTERN.sub(" ", query)
    identifier_text = _mask_filenames(identifier_text)
    for pattern in (
        _QUALIFIED_IDENTIFIER_PATTERN,
        _DOTTED_IDENTIFIER_PATTERN,
        _SNAKE_IDENTIFIER_PATTERN,
        _CONSTANT_IDENTIFIER_PATTERN,
        _TYPE_IDENTIFIER_PATTERN,
    ):
        for match in pattern.finditer(identifier_text):
            if pattern is _DOTTED_IDENTIFIER_PATTERN:
                # The whole qualified spelling, unless its leaf was already
                # named on its own (``\`get\`` and ``Session.get`` in one
                # request describe one symbol).
                if match.group(1) not in identifiers:
                    add(match.group())
                continue
            add(match.group(1) if match.lastindex else match.group())

    return tuple(identifiers)


def should_use_path_index(query: str, intent: QueryIntent | None = None) -> bool:
    """判断是否应该使用路径索引；``intent`` 已知时传入，避免重复分类。

    符号查询不路由到 path index：带符号锚点的查询（即便含扩展名）优先判为 SYMBOL，
    因为它要找的是符号定义而非文件本身。

    Examples:
        >>> should_use_path_index("config.json 在哪里？")
        True

        >>> should_use_path_index("`parse_config` 在 server.py 中注册了哪些路由？")
        False
    """
    if intent is None:
        intent = classify_query_intent(query)
    if intent in {
        QueryIntent.SYMBOL,
        QueryIntent.CALL_CHAIN,
        QueryIntent.REFERENCE,
    }:
        return False
    query_lower = query.lower()
    return bool(
        _has_filename(query)
        or (
            _PATH_KEYWORDS_RE.search(query_lower)
            and not _FEATURE_MARKERS_RE.search(query_lower)
        )
    )
