"""查询分类器 - 按意图分类，支持策略派发"""

from __future__ import annotations

import re
from enum import StrEnum

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
_SNAKE_IDENTIFIER_PATTERN = re.compile(r"[a-z][a-z0-9]*_[a-z0-9_]+")
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
_FILENAME_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_\-]+\.[A-Za-z][A-Za-z0-9]{0,7}")
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
    "call",
    "called",
    "calling",
    "calls",
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
    "pipeline",
    "pipelines",
}

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
    "architecture",
    "implementation",
    "event handling",
    "state management",
    "scheduling",
    "dispatch",
    "mechanism",
    "workflow",
}

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
_PATH_KEYWORDS_RE = _terms_pattern(_PATH_KEYWORDS)
_EXPLICIT_PATH_KEYWORDS_RE = _terms_pattern(
    _EXPLICIT_PATH_KEYWORDS,
    match_ascii_prefix=False,
)
_FEATURE_MARKERS_RE = _terms_pattern(_FEATURE_MARKERS)


# ──────────────────────────────────────────────────────────────────────────────
# 主分类函数
# ──────────────────────────────────────────────────────────────────────────────


def classify_query_intent(query: str) -> QueryIntent:
    """
    按意图分类查询，用于派发检索策略。

    判定优先级（从高到低）：
    1. 标识符超过 2 个或 planner 切出至少 2 个 facet → COMPOUND
    2. 有符号锚点（反引号/snake_case/::）：
       - 调用类动词 → CALL_CHAIN
       - 引用类动词 → REFERENCE
       - 标识符 2 个 → COMPOUND
       - 其余 → SYMBOL
    3. 无符号锚点：
       - 文件名 token（带扩展名）或通用路径词（非功能类）→ PATH
       - 概览词 → OVERVIEW
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

    # 多 facet 是查询本身的广度信号，不依赖是否能从自然语言中提取出代码符号。
    # 放在符号分支外，避免无显式标识符的 issue 被一个 file/config 词缩成 PATH。
    if (
        len(identifiers) > _COMPOUND_IDENTIFIER_LIMIT
        or len(_COMPOUND_PLANNER.plan(query)) >= 3
    ):
        return QueryIntent.COMPOUND

    # 分支1：有符号锚点
    if has_symbol:
        # 提取反引号外的文本，避免符号名本身被动词误匹配
        # 例如 `invoke_handler` 中的 invoke 不应触发 CALL_CHAIN
        text_outside_backticks = re.sub(r"`[^`]+`", "", query_lower)

        # 调用链特征：方向性动词 + 符号（动词在反引号外）
        if _CALL_VERBS_RE.search(text_outside_backticks):
            return QueryIntent.CALL_CHAIN

        # 引用分析：使用/依赖类动词 + 符号（动词在反引号外）
        if _REFERENCE_VERBS_RE.search(text_outside_backticks):
            return QueryIntent.REFERENCE

        if len(identifiers) > 1:
            return QueryIntent.COMPOUND

        # 默认符号定位
        return QueryIntent.SYMBOL

    # 分支2：无符号锚点。用结构信号（文件名 token / 通用路径词）判定，不枚举技术栈。
    has_feature_marker = bool(_FEATURE_MARKERS_RE.search(query_lower))

    # 显式文件名、文件/路径或配置名词才决定 focused PATH 意图；单独的
    # where/在哪里 仍走 FEATURE/OVERVIEW 的 coverage selection。
    if (
        _FILENAME_TOKEN_PATTERN.search(query)
        or _EXPLICIT_PATH_KEYWORDS_RE.search(query_lower)
    ) and not has_feature_marker:
        return QueryIntent.PATH

    # 概览类：架构/机制/流程描述
    if _OVERVIEW_KEYWORDS_RE.search(query_lower):
        return QueryIntent.OVERVIEW

    # 默认功能定位
    return QueryIntent.FEATURE


def extract_code_identifiers(query: str) -> tuple[str, ...]:
    """提取适合精确词法召回的代码标识符，保持查询中的出现顺序。"""
    identifiers: list[str] = []

    def add(value: str) -> None:
        value = value.strip()
        if _IDENTIFIER_PATTERN.fullmatch(value) and value not in identifiers:
            identifiers.append(value)

    for value in re.findall(r"`([^`]+)`", query):
        add(value)

    # 反引号依旧从原文提取；启发式扫描则排除路径和文件名，避免把文件命名
    # 误当成 exact-symbol 证据。路径外的 ``load_config`` 等标识符不受影响。
    identifier_text = _PATH_TOKEN_PATTERN.sub(" ", query)
    identifier_text = _FILENAME_TOKEN_PATTERN.sub(" ", identifier_text)
    for pattern in (
        _QUALIFIED_IDENTIFIER_PATTERN,
        _SNAKE_IDENTIFIER_PATTERN,
        _CONSTANT_IDENTIFIER_PATTERN,
        _TYPE_IDENTIFIER_PATTERN,
    ):
        for match in pattern.finditer(identifier_text):
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
        _FILENAME_TOKEN_PATTERN.search(query)
        or (
            _PATH_KEYWORDS_RE.search(query_lower)
            and not _FEATURE_MARKERS_RE.search(query_lower)
        )
    )
