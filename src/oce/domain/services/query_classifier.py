"""Query intent classification; the intent picks the retrieval strategy."""

from __future__ import annotations

import re
from enum import StrEnum

from oce.domain.chunk.lang import detect_language
from oce.domain.services.query_planner import HeuristicQueryPlanner


class QueryIntent(StrEnum):
    """What a request asks for; each intent has its own retrieval strategy."""

    SYMBOL = "symbol"  # where a function or type is defined
    CALL_CHAIN = "call_chain"  # how one part of the code reaches another
    REFERENCE = "reference"  # where a symbol is used
    PATH = "path"  # where a file lives
    FEATURE = "feature"  # where a behaviour is implemented
    OVERVIEW = "overview"  # how a subsystem is put together
    COMPOUND = "compound"  # several facets or parallel conditions


# ── feature patterns ─────────────────────────────────────────────────────

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

# A token with a file extension (config.json, lib.rs) is strong evidence of
# a file request. The extension must start with a letter so a version such
# as 3.13 is not a file name. Candidates are filtered by the supported
# languages and common project extensions: length alone would drop
# ``build.csproj`` and ``application.properties``, and no filter would take
# ``Session.request`` for a file.
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


# A dotted qualified name (``Context.ShouldBindJSON``,
# ``requests.Session.request``) is a code symbol only when its last segment
# is CamelCase or snake_case; ``example.com`` and ``Foo.bar`` are not.
_DOTTED_IDENTIFIER_PATTERN = re.compile(
    r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*"
    r"\.((?:[A-Z][a-z0-9]+(?:[A-Z][A-Za-z0-9]*)+)|(?:[a-z]+_[a-z0-9_]+)|(?:[A-Z][A-Z0-9]+[a-z][A-Za-z0-9]*))\b"
)
# Paths are masked before snake_case identifiers are read; otherwise
# ``src/message_definition.py`` would yield a ``message_definition`` symbol
# and ``__init__.py`` an ``init__`` one.
_PATH_TOKEN_PATTERN = re.compile(r"(?:[A-Za-z0-9_.\-]+[/\\])+[A-Za-z0-9_.\-]+")

# Call-chain verbs. Only English verbs that express a call relation are
# kept: to / from / path appear in almost every issue text and once routed
# nearly all long English requests to call-chain.
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

# Noun-like path words (file, config, dependency) only signal a file request
# in questions up to this length.
_PATH_KEYWORD_MAX_CHARS = 100
# Minimum word count for a symbol-bearing sentence with architecture words
# to count as an overview.
_OVERVIEW_MIN_WORDS = 8

# Issue-style text: several identifiers, or a planner that cuts several
# clear facets. It describes a compound problem and must not be routed to a
# single symbol's call chain or references because of one verb.
_COMPOUND_IDENTIFIER_LIMIT = 2
_COMPOUND_PLANNER = HeuristicQueryPlanner(max_queries=3)

# Reference/use verbs (one-directional dependency).
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

# Overview/architecture keywords.
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

# Generic file-locating words that hold for any repository.
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

# Strong signals for the focused PATH intent. A plain "where" only says the
# user wants to locate code and may still be a cross-file feature question;
# it may enable the path operator but must not force focused selection.
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

# Feature/implementation markers: with one of these, even a request that
# says "file", "config" or "where" is about behaviour, not a file.
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
    """Compile a keyword set into one matching pattern.

    ASCII words match at a word-boundary prefix, which avoids substring hits
    (how in show, file in profile) while covering inflections (implement to
    implemented, config to configuration). Chinese has no word boundaries and
    matches as a substring, so both languages are judged the same way.
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


# Call verbs must be whole tokens; the common inflections are listed so
# ``call`` does not hit ``callback`` nor ``execute`` hit ``executor``.
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


# ── classification ───────────────────────────────────────────────────────


def classify_query_intent(query: str) -> QueryIntent:
    """Classify a request by intent, highest-priority rule first.

    1. An explicit definition question about a named symbol: SYMBOL.
    2. More than two identifiers, or at least three planner facets: COMPOUND.
    3. An explicit trace: CALL_CHAIN; explicit architecture/lifecycle: OVERVIEW.
    4. A callers question about one symbol: REFERENCE; other call verbs:
       CALL_CHAIN.
    5. With a symbol anchor (backticks, snake_case, ``::``, a qualified name):
       reference, test or implementor questions are REFERENCE; a two-endpoint
       "how" question is CALL_CHAIN; other multi-identifier questions are
       COMPOUND; everything else is SYMBOL.
    6. Without a symbol anchor: overview words give OVERVIEW; a known file
       name or a file/config word in a short non-feature question gives PATH;
       everything else is FEATURE.

    Examples:
        >>> classify_query_intent("`parse_config` 函数在哪里定义？")
        QueryIntent.SYMBOL

        >>> classify_query_intent("前端如何调用后端的 `parse_config`？")
        QueryIntent.CALL_CHAIN

        >>> classify_query_intent("config.json 在哪里？")
        QueryIntent.PATH

        >>> classify_query_intent("`parse_config` 在 server.py 中注册了哪些路由？")
        QueryIntent.SYMBOL  # the symbol wins over the file extension
    """
    query_lower = query.lower()
    identifiers = extract_code_identifiers(query)
    has_symbol = bool(identifiers)

    # Judge verbs on the text outside backticks, paths and file names: the
    # invoke in `invoke_handler` and the execute in src/execute.c are not
    # call-chain verbs.
    text_outside_backticks = re.sub(r"`[^`]+`", "", query_lower)
    text_outside_backticks = _PATH_TOKEN_PATTERN.sub(" ", text_outside_backticks)
    text_outside_backticks = _mask_filenames(text_outside_backticks)

    # "Where is X defined" stays a definition question however many
    # parameter types it names: ``JsonReader``/``TypeToken`` pick the
    # ``fromJson`` overload, they are not further facets.
    if has_symbol and asks_for_definition(text_outside_backticks):
        return QueryIntent.SYMBOL

    # Several facets measure the request's breadth independently of whether
    # a code symbol can be read from the prose. Checked before the symbol
    # branch so an issue without explicit identifiers is not narrowed to
    # PATH by one file/config word.
    if (
        len(identifiers) > _COMPOUND_IDENTIFIER_LIMIT
        or len(_COMPOUND_PLANNER.plan(query)) >= 3
    ):
        return QueryIntent.COMPOUND

    word_count = len(query.split())

    # ``Trace ...`` is the strongest call-chain evidence; a later mention of
    # lifecycle does not change the intent.
    if _TRACE_QUERY.search(text_outside_backticks):
        return QueryIntent.CALL_CHAIN

    # An explicit architecture/lifecycle question may contain process verbs
    # such as execute or flow, yet it asks about the system's coverage, not
    # about one call edge.
    if _EXPLICIT_OVERVIEW_CUES_RE.search(text_outside_backticks):
        return QueryIntent.OVERVIEW

    # "Which places call X" asks where one symbol is used, not for a chain.
    if len(identifiers) == 1 and asks_for_callers(text_outside_backticks):
        return QueryIntent.REFERENCE

    # Call-chain evidence: directional verbs (trace, call, flow). The
    # qualified names in ``Trace requests.request through Session.send``
    # may not read as symbols; the verb alone describes the question.
    if _CALL_VERBS_RE.search(text_outside_backticks):
        return QueryIntent.CALL_CHAIN

    if has_symbol:
        # Use/depend verbs (outside the backticks) next to a symbol.
        if _REFERENCE_VERBS_RE.search(text_outside_backticks):
            return QueryIntent.REFERENCE
        # "Which tests cover X" and "which classes implement X" ask for the
        # places that exercise or extend the symbol: use sites, not its
        # declaration.
        if asks_about_tests(query) or asks_for_implementors(text_outside_backticks):
            return QueryIntent.REFERENCE

        # "How does A reach B": the path between two endpoints is one chain,
        # not two parallel questions.
        if (
            len(identifiers) == 2
            and len(query) <= _QUESTION_MAX_CHARS
            and _HOW_QUERY.search(text_outside_backticks)
        ):
            return QueryIntent.CALL_CHAIN

        if len(identifiers) > 1:
            return QueryIntent.COMPOUND

        # A long architecture description that mentions one symbol
        # (``Engine.ServeHTTP``) is still an overview; only in a short
        # question is the symbol the target.
        if word_count >= _OVERVIEW_MIN_WORDS and _OVERVIEW_KEYWORDS_RE.search(
            text_outside_backticks
        ):
            return QueryIntent.OVERVIEW

        return QueryIntent.SYMBOL

    # No symbol anchor: decide by structural signals (file-name tokens,
    # generic path words) rather than by enumerating technology stacks.
    has_feature_marker = bool(_FEATURE_MARKERS_RE.search(query_lower))

    # Overview before path: the configuration in ``Explain the Gson
    # architecture: ... configuration ...`` is a noun, not a file request.
    if _OVERVIEW_KEYWORDS_RE.search(query_lower):
        return QueryIntent.OVERVIEW

    # An explicit file name decides the focused PATH intent; nouns such as
    # file, path or config only signal a file request in a short question.
    if not has_feature_marker and (
        _has_filename(query)
        or (
            len(query) <= _PATH_KEYWORD_MAX_CHARS
            and _EXPLICIT_PATH_KEYWORDS_RE.search(query_lower)
        )
    ):
        return QueryIntent.PATH

    return QueryIntent.FEATURE


# ``Session.get`` / ``binding.Default``: identifier segments joined by dots. The
# qualifier is kept because it disambiguates same-named declarations; the
# retrieval pipeline derives the leaf to look up.
_DOTTED_QUALIFIED_PATTERN = re.compile(
    r"^[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)+$"
)


def extract_code_identifiers(query: str) -> tuple[str, ...]:
    """Code identifiers suitable for exact recall, in order of appearance.

    A qualified name (``Session.get``, ``a::b::C``) stays one identifier: the
    qualifier is disambiguating evidence and the pipeline derives the leaf,
    otherwise one qualified name would count as two symbols.
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

    # Backticked names are read from the raw text; the heuristic scan masks
    # paths and file names so a file name is not exact-symbol evidence.
    # Identifiers outside paths, such as ``load_config``, are unaffected.
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
    """Whether the path index should recall for this request.

    Pass ``intent`` when already known to avoid classifying twice. Symbol
    requests never use the path index: a request with a symbol anchor is
    SYMBOL even when it names a file, because it wants the declaration.

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
