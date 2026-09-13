"""LLM query rewriting: several angles on one request, above all Chinese to English file names."""

from __future__ import annotations

from loguru import logger

from oce.domain.services.llm.client import LLMClient
from oce.domain.services.llm.prompts import REWRITE_PROMPT_TEMPLATE

# Prompt scaffolding a small model echoes back verbatim. Sent as a query it
# would pollute recall, so the parser drops any line containing it. The
# markers are Chinese because the prompt once was.
_PROMPT_ECHO_MARKERS = (
    "改写策略",
    "用户查询",
    "改写后的查询",
    "搜索关键词",
    "召回率",
    "每行一个",
    "不要编号",
    "文件名版本",
    "英文关键词版本",
    "功能描述版本",
    "查询改写助手",
    "要求:",
)

# A rewrite is a search phrase; a long line is the model explaining itself.
_MAX_REWRITE_CHARS = 80


class QueryRewriter:
    """Rewrite a request into several search variants."""

    def __init__(
        self,
        client: LLMClient,
        model: str | None = None,
        num_rewrites: int = 3,
    ):
        """
        Args:
            client: chat client
            model: model name; None lets the client decide from its credential
            num_rewrites: rewritten queries to generate
        """
        self.client = client
        self.model = model
        self.num_rewrites = num_rewrites

    async def rewrite(self, query: str) -> list[str]:
        """The original query plus its rewrites; the original alone on failure."""
        if not query or not query.strip():
            return [query]

        logger.info("Query rewrite called: query_chars={}", len(query))

        try:
            rewritten_queries = await self._llm_rewrite(query)
            if query not in rewritten_queries:
                rewritten_queries.insert(0, query)
            logger.info("Query rewrite generated {} queries", len(rewritten_queries))
            return rewritten_queries

        except Exception as e:
            logger.warning(
                "Query rewrite failed: {}; using original query only",
                type(e).__name__,
            )
            return [query]

    def _is_valid_rewrite(self, candidate: str) -> bool:
        """Whether an output line is a usable rewrite: short and free of prompt scaffolding."""
        if len(candidate) > _MAX_REWRITE_CHARS:
            return False
        if len(candidate) < 3:
            return False
        return not any(marker in candidate for marker in _PROMPT_ECHO_MARKERS)

    async def _llm_rewrite(self, query: str) -> list[str]:
        """The model's rewrites, one per output line."""
        prompt = REWRITE_PROMPT_TEMPLATE.format(
            num_rewrites=self.num_rewrites, query=query
        )

        messages = [{"role": "user", "content": prompt}]

        response = await self.client.chat(
            messages,
            model=self.model,
            temperature=0.2,
        )

        rewritten_queries: list[str] = []
        rejected: list[str] = []

        for line in response.strip().split("\n"):
            # Strip numbering, list markers and whitespace.
            cleaned = line.strip()
            cleaned = cleaned.lstrip("0123456789.-*• \t")
            if not cleaned:
                continue

            if self._is_valid_rewrite(cleaned):
                rewritten_queries.append(cleaned)
            else:
                rejected.append(cleaned)

        rewritten_queries = rewritten_queries[: self.num_rewrites]

        if rejected:
            logger.warning("Query rewrite dropped {} invalid lines", len(rejected))

        if not rewritten_queries:
            logger.warning("LLM returned no valid rewritten queries")

        return rewritten_queries
