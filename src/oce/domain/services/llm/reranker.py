"""Chat-LLM reranker.

A small model reorders the candidates where the embedding falls short,
above all for Chinese requests against English identifiers and file names.
"""

from __future__ import annotations

import asyncio
import re

from loguru import logger

from oce.domain.services.llm.client import LLMClient
from oce.domain.services.llm.prompts import RERANK_SYSTEM_PROMPT, RERANK_USER_TEMPLATE
from oce.domain.services.search import SearchHit


class LLMReranker:
    """Semantic reranker backed by a chat model."""

    def __init__(
        self,
        client: LLMClient,
        model: str | None = None,
        max_candidates: int = 50,
        output_top_k: int = 10,
        snippet_chars: int = 1600,
        timeout_seconds: float = 15.0,
    ):
        """
        Args:
            client: chat client
            model: model name; None lets the client decide from its credential
            max_candidates: most candidates sent to the model (cost bound)
            output_top_k: most candidates the model may move to the head
            snippet_chars: code characters per candidate. Paths alone reduce
                reranking to file-name matching; symbol and call-chain
                requests cannot be judged without the body.
            timeout_seconds: end-to-end limit; a timeout keeps the input order
        """
        if max_candidates < 1:
            raise ValueError("max_candidates must be positive")
        if output_top_k < 1:
            raise ValueError("output_top_k must be positive")
        if snippet_chars < 1:
            raise ValueError("snippet_chars must be positive")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.client = client
        self.model = model
        self.max_candidates = max_candidates
        self.output_top_k = output_top_k
        self.snippet_chars = snippet_chars
        self.timeout_seconds = timeout_seconds

    async def rerank(
        self,
        query: str,
        candidates: list[SearchHit],
    ) -> list[SearchHit]:
        """The same candidates with the model's picks first and the rest in input order."""
        if not candidates:
            return []

        # Limit the model window first. output_top_k may be configured larger than
        # max_candidates, but the prompt must never request more ids than it contains.
        candidates_subset = candidates[: self.max_candidates]
        promotion_count = min(self.output_top_k, len(candidates_subset))

        logger.info(
            "LLM rerank called: query_chars={}, candidates={}, top_k={}",
            len(query),
            len(candidates),
            promotion_count,
        )

        try:
            # The order comes back by index, not by path: one file may
            # contribute several chunks, and keying by path would fold them
            # into one exactly when a symbol request needs them apart.
            async with asyncio.timeout(self.timeout_seconds):
                order = await self._llm_rerank(
                    query,
                    candidates_subset,
                    promotion_count,
                )
            reranked_results = [candidates_subset[i] for i in order]

            # The reranker reorders but never prunes: unpicked candidates
            # inside the window and everything outside it follow in input
            # order, and the selector decides coverage and budget.
            chosen = set(order)
            reranked_results.extend(
                candidate
                for index, candidate in enumerate(candidates_subset)
                if index not in chosen
            )
            reranked_results.extend(candidates[len(candidates_subset) :])
            return reranked_results

        except TimeoutError:
            logger.warning(
                "LLM rerank exceeded {:.1f}s; preserving retrieval order",
                self.timeout_seconds,
            )
            return candidates
        except Exception as e:
            logger.warning(
                "LLM rerank failed: {}; falling back to original order",
                type(e).__name__,
            )
            return candidates

    def _format_candidate(self, index: int, candidate: SearchHit) -> str:
        """Render one candidate as a ``<candidate>`` element with path, lines and code.

        The body is required: paths alone cannot show a declaration or a call.
        Closing tags instead of markdown fences, because a .md candidate
        carries its own ``` and fences would tear thirty candidates apart.
        """
        path = candidate.path.replace('"', "&quot;")
        start = candidate.start_line
        end = candidate.end_line
        lines_attr = f' lines="{start}-{end}"' if start and end else ""
        # The scope chain tells the judge which class a truncated method belongs
        # to, which is what a shorter snippet budget would otherwise lose.
        if candidate.context:
            context = candidate.context.replace('"', "&quot;")
            lines_attr += f' context="{context}"'

        snippet = candidate.content.strip()
        if len(snippet) > self.snippet_chars:
            snippet = snippet[: self.snippet_chars] + "\n…"
        # A closing tag inside the body would end the candidate early.
        snippet = snippet.replace("</candidate", "<\\/candidate")

        open_tag = f'<candidate id="{index}" path="{path}"{lines_attr}>'
        if not snippet:
            return f"{open_tag}</candidate>"
        return f"{open_tag}\n{snippet}\n</candidate>"

    async def _llm_rerank(
        self, query: str, candidates: list[SearchHit], top_k: int
    ) -> list[int]:
        """Zero-based candidate indices in the model's order, deduplicated and in range."""
        candidates_text = "\n".join(
            self._format_candidate(i + 1, c) for i, c in enumerate(candidates)
        )

        messages = [
            {"role": "system", "content": RERANK_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": RERANK_USER_TEMPLATE.format(
                    query=query,
                    count=len(candidates),
                    candidates=candidates_text,
                    top_k=top_k,
                ),
            },
        ]

        response = await self.client.chat(
            messages,
            model=self.model,
            temperature=0.1,
            max_tokens=min(512, max(128, top_k * 12)),
        )

        order: list[int] = []
        seen: set[int] = set()

        for line in response.strip().splitlines():
            # Accept "1", "1.", "- 1", "[1] path" and similar: the first integer.
            match = re.search(r"\d+", line)
            if match is None:
                continue
            index = int(match.group()) - 1
            if 0 <= index < len(candidates) and index not in seen:
                seen.add(index)
                order.append(index)

        order = order[:top_k]

        if not order:
            logger.warning("LLM returned no valid rerank indices")
            return list(range(min(top_k, len(candidates))))

        return order
