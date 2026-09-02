"""Reranker 领域服务 - 召回精排

解决「单篇多相关」：对 store 召回的结果做二次精排（跨文档比较）。
NoopReranker 用于关闭重排（起步阶段退化为纯召回排序）。
"""

from __future__ import annotations

from typing import Protocol

from oce.domain.services.search import SearchHit


class Reranker(Protocol):
    """候选保真的重排器协议。

    实现可替换顺序或更新分数，但必须保留每个输入候选且不得引入新候选；
    去重、覆盖度与上下文预算属于后续 Selector。
    """

    async def rerank(self, query: str, hits: list[SearchHit]) -> list[SearchHit]:
        """对召回结果精排，返回包含同一候选集的新顺序。"""
        ...


class NoopReranker:
    """不重排（原样返回）"""

    async def rerank(self, query: str, hits: list[SearchHit]) -> list[SearchHit]:
        return hits
