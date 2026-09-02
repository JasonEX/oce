"""意图驱动的检索策略决策表

根据查询意图选择最优的检索策略组合
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
    # 保留原查询中的方向和边界信息，交给 LLM 判断调用关系。
    QueryIntent.CALL_CHAIN: RetrievalStrategy(
        enable_path_index=False,
        enable_query_rewrite=False,
    ),
    # R (REFERENCE): 引用/使用位置查询
    # 策略：查询改写 + 中等块数
    QueryIntent.REFERENCE: RetrievalStrategy(
        enable_path_index=False,
        enable_query_rewrite=True,
    ),
    # P (PATH): 文件路径查询
    # 文件语义改写补足中英文差异，路径索引负责召回，高置信结果无需 LLM。
    QueryIntent.PATH: RetrievalStrategy(
        enable_path_index=True,
        enable_query_rewrite=True,
        selection_mode=SelectionMode.FOCUSED,
    ),
    # F (FEATURE): 功能实现查询
    # 功能描述需要跨中英文术语召回，候选歧义时再交给 LLM。
    QueryIntent.FEATURE: RetrievalStrategy(
        enable_path_index=False,
        enable_query_rewrite=True,
    ),
    # O (OVERVIEW): 架构/机制概览查询
    # 策略：LLM 重排（理解架构描述）
    QueryIntent.OVERVIEW: RetrievalStrategy(
        enable_path_index=False,
        enable_query_rewrite=False,
    ),
    # M (COMPOUND): 复合查询
    # 策略：查询改写 + LLM 重排（处理多条件）
    QueryIntent.COMPOUND: RetrievalStrategy(
        enable_path_index=False,
        enable_query_rewrite=True,
    ),
}


def get_strategy(intent: QueryIntent) -> RetrievalStrategy:
    """获取意图对应的检索策略

    Args:
        intent: 查询意图

    Returns:
        检索策略配置
    """
    return STRATEGY_TABLE.get(intent, STRATEGY_TABLE[QueryIntent.FEATURE])


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
