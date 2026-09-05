"""意图驱动的检索策略决策表。

策略决定确定性的召回开关；两种 reranker 的授权与逐查询路由由
``plan_rerank`` 统一判断。
"""

from __future__ import annotations

from dataclasses import dataclass

from oce.domain.services.query_classifier import QueryIntent
from oce.domain.services.selector.protocols import SelectionMode
from oce.shared.config.settings import RerankPolicy


@dataclass(frozen=True)
class RetrievalStrategy:
    """检索策略配置"""

    enable_path_index: bool = False
    enable_query_rewrite: bool = False
    enable_lexical_recall: bool = False
    expand_related_definitions: bool = False
    # 关系车道：谁调用、谁实现/继承、哪些测试覆盖、哪个入口文件转出。
    expand_callers: bool = False
    expand_implementations: bool = False
    expand_tests: bool = False
    expand_reexports: bool = False
    selection_mode: SelectionMode = SelectionMode.COVERAGE

    @property
    def expands_relations(self) -> bool:
        return (
            self.expand_related_definitions
            or self.expand_callers
            or self.expand_implementations
            or self.expand_tests
            or self.expand_reexports
        )


# 决策表：意图 → 检索策略
STRATEGY_TABLE: dict[QueryIntent, RetrievalStrategy] = {
    # S (SYMBOL): 符号定义查询
    # 符号名应在正文中定位；路径语义会把同名引用、模型和 DAO 提到定义前面。
    # 定义之后紧跟的问题是谁调用它、谁实现它、哪些测试覆盖它、从哪里导出。
    QueryIntent.SYMBOL: RetrievalStrategy(
        enable_path_index=False,
        enable_query_rewrite=True,
        expand_callers=True,
        expand_implementations=True,
        expand_tests=True,
        expand_reexports=True,
        selection_mode=SelectionMode.FOCUSED,
    ),
    # C (CALL_CHAIN): 调用链查询
    # 不改写：原查询中的方向和边界信息是后续重排判断调用关系的依据。词法
    # occurrence 与二跳定义共同补足 dense 不掌握的结构关系。
    QueryIntent.CALL_CHAIN: RetrievalStrategy(
        enable_lexical_recall=True,
        expand_related_definitions=True,
        expand_callers=True,
        expand_tests=True,
    ),
    # R (REFERENCE): 引用/使用位置查询，改写补充同义调用方式。exact 召回已含
    # 转出与继承行，调用方与测试小节补足窗口外的使用位置。
    QueryIntent.REFERENCE: RetrievalStrategy(
        enable_query_rewrite=True,
        enable_lexical_recall=True,
        expand_callers=True,
        expand_tests=True,
    ),
    # P (PATH): 文件路径查询
    # 文件语义改写补足中英文差异，路径索引负责召回，高置信结果无需 LLM。
    QueryIntent.PATH: RetrievalStrategy(
        enable_path_index=True,
        enable_query_rewrite=True,
        selection_mode=SelectionMode.FOCUSED,
    ),
    # F (FEATURE): 功能实现查询，功能描述需要跨中英文术语召回。
    QueryIntent.FEATURE: RetrievalStrategy(
        enable_query_rewrite=True,
        enable_lexical_recall=True,
        expand_related_definitions=True,
        expand_tests=True,
    ),
    # O (OVERVIEW): 架构/机制概览查询以 dense 为主，词法结果补充模块和文档术语。
    QueryIntent.OVERVIEW: RetrievalStrategy(
        enable_lexical_recall=True,
        expand_related_definitions=True,
    ),
    # M (COMPOUND): 复合查询，改写把并列条件拆成可分别召回的角度。
    QueryIntent.COMPOUND: RetrievalStrategy(
        enable_query_rewrite=True,
        enable_lexical_recall=True,
        expand_tests=True,
    ),
}


def get_strategy(intent: QueryIntent) -> RetrievalStrategy:
    return STRATEGY_TABLE[intent]


@dataclass(frozen=True)
class RerankDecision:
    """Which rerankers run for one candidate set, and the evidence that decided it."""

    dedicated: bool
    llm: bool
    reason: str

    @property
    def route(self) -> str:
        """Audit label: the rerankers applied, or the skip reason when none ran."""
        applied = [
            name
            for name, on in (("dedicated", self.dedicated), ("llm", self.llm))
            if on
        ]
        if applied:
            return "+".join(applied)
        return f"skip:{self.reason}"


def plan_rerank(
    intent: QueryIntent,
    candidate_count: int,
    *,
    has_exact_hits: bool = False,
    has_path_hits: bool = False,
    definition_sites: int = 0,
    head_slots: int = 3,
    rerank_ambiguous_definitions: bool = False,
    dedicated_enabled: bool = True,
    llm_enabled: bool = True,
    dedicated_policy: RerankPolicy = "adaptive",
    llm_policy: RerankPolicy = "adaptive",
) -> RerankDecision:
    """Decide which rerankers a candidate set benefits from.

    Both models share the same deterministic evidence. Retrieval scores are
    deliberately excluded: dense cosine, RRF, exact, path, and reranker scores do
    not share a calibrated scale, so a skip is only taken when a structural
    operator has already answered the question. ``definition_sites`` is the
    number of places declaring the queried name; it only matters when the
    deployment opted into reranking ambiguous symbol tails. ``enabled`` flags
    authorize the corresponding stage; a policy can never switch on a model
    that is disabled.
    """
    for name, policy in (
        ("dedicated rerank", dedicated_policy),
        ("LLM rerank", llm_policy),
    ):
        if policy not in ("adaptive", "always"):
            raise ValueError(f"Unsupported {name} policy: {policy}")

    if candidate_count < 2:
        return RerankDecision(False, False, "too_few_candidates")

    if (
        intent == QueryIntent.SYMBOL
        and has_exact_hits
        and rerank_ambiguous_definitions
        and definition_sites > head_slots
    ):
        # More declarations of the name than protected head slots: the head
        # stays deterministic, a dedicated model may order the overflow.
        adaptive = (True, False, "ambiguous_definition")
    elif intent == QueryIntent.SYMBOL and has_exact_hits:
        adaptive = (False, False, "exact_definition")
    elif intent == QueryIntent.PATH and has_path_hits:
        adaptive = (False, False, "path_evidence")
    elif intent in (QueryIntent.SYMBOL, QueryIntent.PATH):
        adaptive = (True, True, "no_structural_evidence")
    elif intent == QueryIntent.REFERENCE:
        # Reference questions want occurrence coverage; a global semantic judge
        # would collapse the list onto one implementation.
        adaptive = (True, False, "reference_keep_coverage")
    else:
        adaptive = (True, True, "semantic")

    dedicated = dedicated_enabled and (dedicated_policy == "always" or adaptive[0])
    llm = llm_enabled and (llm_policy == "always" or adaptive[1])
    reason = adaptive[2]
    if not dedicated and not llm and not (dedicated_enabled or llm_enabled):
        reason = "no_reranker_enabled"
    return RerankDecision(dedicated, llm, reason)
