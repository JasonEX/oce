"""意图驱动的检索策略决策表。

策略只决定确定性的召回开关；chat LLM 是否参与重排由 ``should_use_llm_rerank``
按候选证据单独判断。
"""

from __future__ import annotations

from dataclasses import dataclass

from oce.domain.services.query_classifier import QueryIntent
from oce.domain.services.selector.protocols import SelectionMode

_SEMANTIC_INTENTS = {
    QueryIntent.CALL_CHAIN,
    QueryIntent.FEATURE,
    QueryIntent.OVERVIEW,
    QueryIntent.COMPOUND,
}


@dataclass(frozen=True)
class RetrievalStrategy:
    """检索策略配置"""

    enable_path_index: bool = False
    enable_query_rewrite: bool = False
    selection_mode: SelectionMode = SelectionMode.COVERAGE


# 决策表：意图 → 检索策略
STRATEGY_TABLE: dict[QueryIntent, RetrievalStrategy] = {
    # S (SYMBOL): 符号定义查询
    # 符号名应在正文中定位；路径语义会把同名引用、模型和 DAO 提到定义前面。
    QueryIntent.SYMBOL: RetrievalStrategy(
        enable_path_index=False,
        enable_query_rewrite=True,
        selection_mode=SelectionMode.FOCUSED,
    ),
    # C (CALL_CHAIN): 调用链查询
    # 不改写：原查询中的方向和边界信息是后续重排判断调用关系的依据。
    QueryIntent.CALL_CHAIN: RetrievalStrategy(),
    # R (REFERENCE): 引用/使用位置查询，改写补充同义调用方式。
    QueryIntent.REFERENCE: RetrievalStrategy(enable_query_rewrite=True),
    # P (PATH): 文件路径查询
    # 文件语义改写补足中英文差异，路径索引负责召回，高置信结果无需 LLM。
    QueryIntent.PATH: RetrievalStrategy(
        enable_path_index=True,
        enable_query_rewrite=True,
        selection_mode=SelectionMode.FOCUSED,
    ),
    # F (FEATURE): 功能实现查询，功能描述需要跨中英文术语召回。
    QueryIntent.FEATURE: RetrievalStrategy(enable_query_rewrite=True),
    # O (OVERVIEW): 架构/机制概览查询，原描述本身就是最好的召回文本。
    QueryIntent.OVERVIEW: RetrievalStrategy(),
    # M (COMPOUND): 复合查询，改写把并列条件拆成可分别召回的角度。
    QueryIntent.COMPOUND: RetrievalStrategy(enable_query_rewrite=True),
}


def get_strategy(intent: QueryIntent) -> RetrievalStrategy:
    return STRATEGY_TABLE[intent]


def should_use_llm_rerank(
    intent: QueryIntent,
    candidate_count: int,
    *,
    policy: str = "adaptive",
    has_exact_hits: bool = False,
    has_path_hits: bool = False,
) -> bool:
    """Decide whether a candidate set benefits from global semantic judging.

    Retrieval scores are deliberately excluded: dense cosine, RRF, exact, path, and
    dedicated-reranker scores do not share a calibrated scale. ``adaptive`` instead
    uses stable structural evidence. Exact symbol and path hits already have a strong
    deterministic operator; reference queries preserve occurrence coverage. Feature,
    flow, overview, and compound questions benefit from comparing snippet meaning.
    """
    if candidate_count < 2:
        return False
    if policy == "always":
        return True
    if policy != "adaptive":
        raise ValueError(f"Unsupported LLM rerank policy: {policy}")
    if intent == QueryIntent.REFERENCE:
        return False
    if intent == QueryIntent.SYMBOL:
        return not has_exact_hits
    if intent == QueryIntent.PATH:
        return not has_path_hits
    return intent in _SEMANTIC_INTENTS
