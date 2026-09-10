"""Deterministic routing separates mentioned entities from requested evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from oce.domain.services.query_evidence import QueryEvidence, extract_query_evidence
from oce.domain.services.query_symbols import mask_code


class QueryIntent(StrEnum):
    """查询意图类型，用于派发检索策略"""

    SYMBOL = "symbol"  # 符号定位：某函数/类型在哪里定义
    CALL_CHAIN = "call_chain"  # 调用链分析：前端如何调用某后端命令
    REFERENCE = "reference"  # 引用分析：某符号在别处如何被使用
    PATH = "path"  # 路径定位：某配置文件在哪里
    FEATURE = "feature"  # 功能定位：某功能的实现在哪里
    OVERVIEW = "overview"  # 架构理解：某子系统的实现与事件处理
    COMPOUND = "compound"  # 复合查询：多 facet 或并列条件


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

# 决定 focused PATH 意图的强信号。普通 "where/在哪里" 只说明用户想定位代码，
# 仍可能是跨文件功能问题，不能单独启用 path operator 或 focused selection。
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


_OVERVIEW_KEYWORDS_RE = _terms_pattern(_OVERVIEW_KEYWORDS)
_EXPLICIT_OVERVIEW_CUES_RE = _terms_pattern(_EXPLICIT_OVERVIEW_CUES)
_EXPLICIT_PATH_KEYWORDS_RE = _terms_pattern(
    _EXPLICIT_PATH_KEYWORDS,
    match_ascii_prefix=False,
)
_FEATURE_MARKERS_RE = _terms_pattern(_FEATURE_MARKERS)


_REQUEST_START = re.compile(
    r"(?i)^\s*(?:please\s+)?(?:where|which|what|who|how|why|find|locate|show|list|trace|explain|describe|compare|identify|walk\s+through)\b|^\s*(?:找到|查找|定位|列出|追踪|跟踪|解释|哪些|谁|如何|怎样|怎么)"
)
_TEST_QUERY = re.compile(
    r"(?i)\b(?:which|what|find|locate|show|list|identify|where)\b[^.?\n]{0,80}\btests?\b"
    r"|\btests?\s+(?:for|of|covering|exercising|that\s+cover)\b"
    r"|\b(?:unit|integration|regression)\s+tests?\b|\btest\s+cases?\b|\bfixtures?\s+for\b"
    r"|有(?:没有)?测试|测试用例|单元测试|哪个测试|哪些测试|测试在哪|相关测试|覆盖.{0,80}测试|测试.{0,80}覆盖"
)
_IMPLEMENTORS_QUERY = re.compile(
    r"(?i)\b(?:which|what|all|list|every|find|locate)\b[^.?\n]{0,60}\b(?:implement|extend|subclass|override|inherit|derive)"
    r"|\bimplemented\s+(?:for|by|on)\b|\b(?:implementations?|implementors|subclasses|subtypes)\s+(?:of|for)\b"
    r"|哪些.{0,30}(?:实现|继承|派生|子类)|实现者|子类|为.{0,60}实现|对.{0,60}实现"
)
_CALLERS_QUERY = re.compile(
    r"(?i)\b(?:which|what|who|where|all|list|every|find|locate|show)\b[^.?\n]{0,60}\b(?:calls?|called|calling|invokes?|invoked|triggers?|triggered)\b"
    r"|\bcallers?\b|哪些.{0,30}(?:调用|触发|执行)|谁.{0,20}调用|被.{0,20}调用|调用方|调用者"
)
_REFERENCE_QUERY = re.compile(
    r"(?i)\b(?:us(?:e[ds]?|ing|ages?)|references?|referenced|imports?|depends?|consumes?)\b|使用|被用|引用|导入|依赖|消费"
)
_DEFINITION_QUERY = re.compile(
    r"(?i)\b(?:defin(?:ed|ition|itions)|declarations?|overloads?)\b|定义|声明|重载"
)
_LOCATION_QUERY = re.compile(
    r"(?i)\b(?:where|locate|find|show)\b|找到|查找|定位|列出|在哪里|在哪|位置|实现文件"
)
_IMPLEMENTATION_QUERY = re.compile(r"(?i)\bimplemented\b|如何实现|的实现")
_FLOW_QUERY = re.compile(
    r"(?i)\b(?:trac(?:e|es|ing)|chain|flow|reach(?:es)?|call[- ]?(?:chain|path))\b|调用链|调用路径|追踪|跟踪|到达"
)
_CALL_QUERY = re.compile(
    r"(?i)\b(?:calls?|called|calling|invokes?|invoked|triggers?|triggered|executes?|executed)\b|调用|触发|执行"
)
_ASKS_HOW = re.compile(
    r"(?i)\b(?:how|why|which|what|where|explain|describe|trace|walk\s+through)\b|如何|怎么|怎样|为什么|解释|说明|哪些|哪个|什么"
)


@dataclass(frozen=True)
class QueryRoute:
    intent: QueryIntent = QueryIntent.FEATURE
    # Only the requested subjects, not parameter types or incidental mentions.
    targets: tuple[str, ...] = ()
    tests_requested: bool = False
    implementations_requested: bool = False
    path_requested: bool = False
    overrides_requested: bool = False


def asks_how(query: str) -> bool:
    return _ASKS_HOW.search(query) is not None


def asks_about_tests(query: str) -> bool:
    return _TEST_QUERY.search(query) is not None


def asks_for_implementors(query: str) -> bool:
    return _IMPLEMENTORS_QUERY.search(query) is not None


def _subjects(
    text: str, names: tuple[str, ...], *, definition: bool
) -> tuple[str, ...]:
    if len(names) < 2:
        return names
    # A signature or an overload noun names its function, with the other
    # entities constraining that declaration. Text order alone cannot do this.
    for name in names:
        if definition and re.search(
            r"(?:definition|declaration)\s+of\s+`?"
            + re.escape(name)
            + r"`?\s+(?:with|taking)\b",
            text,
            re.I,
        ):
            return (name,)
        if definition and re.search(
            re.escape(name) + r"`?\s*(?:\(|(?:的)?\s*(?:overload\b|重载))", text, re.I
        ):
            return (name,)
    if not definition:
        # The requested trait is before "implemented for T" or immediately
        # after the relational noun. This also handles "T 对 Trait 的实现".
        for name in names:
            escaped = r"`?" + re.escape(name) + r"`?"
            if re.search(
                escaped
                + r"\s*(?:implement(?:ed|ation)\s+(?:for|by)|的?实现(?:者)?[？?。.]?$)",
                text,
                re.I,
            ) or re.search(
                r"(?:implementations?\s+of|实现(?:了)?|继承(?:了)?)\s*" + escaped,
                text,
                re.I,
            ):
                return (name,)
        return names
    return names


def _chain_subjects(text: str, names: tuple[str, ...]) -> tuple[str, ...]:
    """Respect directed endpoints; an intermediate name is not the destination."""
    if names and re.search(
        r"(?i)\b(?:through|via)\b|经过|经由", text[: text.index(names[0])]
    ):
        # The first recognized name occurs only after an unrecognized start
        # and a traversal marker. It is an intermediate, not a starting anchor.
        return ()
    if len(names) < 2:
        return names
    spelling = "|".join(
        re.escape(name) for name in sorted(names, key=len, reverse=True)
    )
    after = r"\s*`?(" + spelling + r")(?![A-Za-z0-9_$])"
    source = re.search(r"(?:\b(?i:from)\b|从)" + after, text)
    destination = re.search(
        r"(?:\b(?i:to|into|reach|reaches|call|calls|invoke|invokes)\b|到达|→)" + after,
        text,
    )
    start = source.group(1) if source else names[0]
    if destination and destination.group(1) != start:
        return (start, destination.group(1))
    if len(names) == 2:
        return (start, next(name for name in names if name != start))
    return (start,)


def route_query(query: str, evidence: QueryEvidence) -> QueryRoute:
    # Multiple actual requests are compound; an informational preamble does
    # not create another request. A traceback remains a multi-location task.
    clauses = [
        part.strip()
        for part in re.split(r"[.!?。！？](?:\s+|$)|\n+", query)
        if part.strip()
    ]
    requests = [part for part in clauses if _REQUEST_START.search(part)]
    if evidence.frames or "```" in query or len(requests) > 1:
        return QueryRoute(QueryIntent.COMPOUND)
    request = requests[0] if len(requests) == 1 else query
    names = tuple(name for name in evidence.identifiers if name in request)
    text = mask_code(request, evidence.identifiers).lower()
    tests = asks_about_tests(text)
    implementors = asks_for_implementors(text)
    if tests or (names and (implementors or _CALLERS_QUERY.search(text))):
        return QueryRoute(
            QueryIntent.REFERENCE,
            _subjects(request, names, definition=False),
            tests,
            implementors,
            overrides_requested=bool(
                re.search(r"(?i)\boverrid(?:e[sn]?|ing|den)\b|覆写|重写", text)
            ),
        )
    if (
        names
        and _DEFINITION_QUERY.search(text)
        and (
            _LOCATION_QUERY.search(text)
            or re.search(
                r"^\s*(?:the\s+|(?:函数|方法|类型)?的?\s*)?(?:definition|declaration|定义|声明)",
                text,
                re.I,
            )
        )
    ):
        return QueryRoute(
            QueryIntent.SYMBOL, _subjects(request, names, definition=True)
        )
    if _TRACE_QUERY.search(text):
        return QueryRoute(QueryIntent.CALL_CHAIN, _chain_subjects(request, names))
    if _EXPLICIT_OVERVIEW_CUES_RE.search(text):
        return QueryRoute(QueryIntent.OVERVIEW)
    if _FLOW_QUERY.search(text) or _CALL_QUERY.search(text):
        return QueryRoute(QueryIntent.CALL_CHAIN, _chain_subjects(request, names))
    if names and _REFERENCE_QUERY.search(text):
        return QueryRoute(
            QueryIntent.REFERENCE, _subjects(request, names, definition=False)
        )
    path = not _FEATURE_MARKERS_RE.search(text) and (
        evidence.has_path_evidence
        or (len(request) <= 100 and _EXPLICIT_PATH_KEYWORDS_RE.search(text))
    )
    if path and not any("`" + name + "`" in request for name in names):
        return QueryRoute(QueryIntent.PATH, path_requested=True)
    # A location word in a larger feature description does not ask for each
    # mentioned name's declaration. Keep its SQL recall, but keep coverage too.
    if names and (
        (_LOCATION_QUERY.search(text) and not _FEATURE_MARKERS_RE.search(text))
        or _IMPLEMENTATION_QUERY.search(text)
        or not re.search(r"[A-Za-z\u4e00-\u9fff]", text)
    ):
        return QueryRoute(QueryIntent.SYMBOL, names)
    if re.search(r"(?i)\bcompare\b|比较|对比", text) and len(names) > 1:
        return QueryRoute(QueryIntent.COMPOUND)
    if _OVERVIEW_KEYWORDS_RE.search(text):
        return QueryRoute(QueryIntent.OVERVIEW)
    path = not _FEATURE_MARKERS_RE.search(text) and (
        evidence.has_path_evidence
        or (len(request) <= 100 and _EXPLICIT_PATH_KEYWORDS_RE.search(text))
    )
    if path and not names:
        return QueryRoute(QueryIntent.PATH, path_requested=True)
    return QueryRoute(path_requested=bool(evidence.has_path_evidence and not names))


def classify_query_intent(query: str) -> QueryIntent:
    return route_query(query, extract_query_evidence(query)).intent


def should_use_path_index(query: str) -> bool:
    route = route_query(query, extract_query_evidence(query))
    return route.path_requested
