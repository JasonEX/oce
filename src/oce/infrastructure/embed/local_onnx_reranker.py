"""In-process cross-encoder reranker over an ONNX export (no data egress).

The API reranker sends the query and candidate source to a provider. This
adapter runs a sequence-classification cross-encoder such as
``jinaai/jina-reranker-v2-base-multilingual`` (``onnx/model_int8.onnx`` plus
``tokenizer.json``) on the CPU instead. Measured on a 16-core laptop CPU:
20 candidates of about 320 tokens score in roughly 1.2 s at batch size 4,
so the window is deliberately small; the head slots the pipeline applies
afterwards only need the first ten or so candidates ordered well.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from loguru import logger

from oce.domain.services.search import SearchHit


def _sigmoid(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError("local reranker returned a non-finite score")
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    exp_value = math.exp(value)
    return exp_value / (1.0 + exp_value)


def _default_threads() -> int:
    """Half the logical cores, at most eight.

    On hybrid big/little CPUs onnxruntime's default of one thread per logical
    core was slower than eight threads (2.5 s vs 1.4 s for twenty pairs).
    """
    import os

    return max(1, min(8, (os.cpu_count() or 8) // 2))


class LocalOnnxReranker:
    def __init__(
        self,
        *,
        model_dir: str | Path,
        candidates: int = 20,
        max_doc_chars: int = 1_200,
        max_query_chars: int = 2_400,
        max_tokens: int = 512,
        batch_size: int = 4,
        threads: int = 0,
        model_file: str = "model_int8.onnx",
    ) -> None:
        if candidates < 1 or batch_size < 1 or max_tokens < 16:
            raise ValueError(
                "local reranker window, batch and token limits must be positive"
            )
        if max_doc_chars < 1 or max_query_chars < 1:
            raise ValueError("local reranker character limits must be positive")
        self._model_dir = Path(model_dir).expanduser()
        self._model_file = model_file
        self._candidates = candidates
        self._max_doc_chars = max_doc_chars
        self._max_query_chars = max_query_chars
        self._max_tokens = max_tokens
        self._batch_size = batch_size
        self._threads = threads
        self._session: Any = None
        self._tokenizer: Any = None
        self._input_names: tuple[str, ...] = ()
        self._load_lock = asyncio.Lock()
        self._load_failed = False

    @property
    def model_name(self) -> str:
        return f"local:{self._model_dir.name}"

    def _load(self) -> None:
        """Import and build the runtime lazily; missing wheels or files are
        reported once, with the fix, rather than failing every request."""
        try:
            import numpy  # noqa: F401
            import onnxruntime
            from tokenizers import Tokenizer
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise RuntimeError(
                "RERANK_PROVIDER=local needs the 'local-rerank' extra "
                "(uv sync --extra local-rerank)"
            ) from exc
        model_path = self._model_dir / self._model_file
        tokenizer_path = self._model_dir / "tokenizer.json"
        if not model_path.is_file() or not tokenizer_path.is_file():
            raise RuntimeError(
                f"local reranker model files missing under {self._model_dir}: "
                f"expected {self._model_file} and tokenizer.json"
            )
        options = onnxruntime.SessionOptions()
        threads = self._threads or _default_threads()
        options.intra_op_num_threads = threads
        session = onnxruntime.InferenceSession(
            str(model_path), options, providers=["CPUExecutionProvider"]
        )
        tokenizer = Tokenizer.from_file(str(tokenizer_path))
        tokenizer.enable_truncation(max_length=self._max_tokens)
        pad_id = tokenizer.token_to_id("<pad>")
        tokenizer.enable_padding(pad_id=pad_id if pad_id is not None else 0)
        self._input_names = tuple(item.name for item in session.get_inputs())
        self._session = session
        self._tokenizer = tokenizer
        logger.info("Loaded local ONNX reranker from {}", self._model_dir)

    async def ensure_loaded(self) -> None:
        if self._session is not None:
            return
        async with self._load_lock:
            if self._session is None:
                await asyncio.to_thread(self._load)

    async def rerank(self, query: str, hits: list[SearchHit]) -> list[SearchHit]:
        if not hits:
            return []
        if self._load_failed:
            return hits
        try:
            await self.ensure_loaded()
        except Exception as exc:
            self._load_failed = True
            logger.warning("Local reranker unavailable; using retrieval order: {}", exc)
            return hits
        window = hits[: self._candidates]
        query_text = query[: self._max_query_chars]
        documents = [self._document_text(hit) for hit in window]
        try:
            scores = await asyncio.to_thread(self._score, query_text, documents)
        except Exception as exc:
            logger.warning("Local reranker failed; using retrieval order: {}", exc)
            return hits
        order = sorted(range(len(window)), key=lambda index: -scores[index])
        promoted = [replace(window[index], score=scores[index]) for index in order]
        return [*promoted, *hits[len(window) :]]

    def _score(self, query: str, documents: Sequence[str]) -> list[float]:
        import numpy as np

        scores: list[float] = []
        for start in range(0, len(documents), self._batch_size):
            batch = documents[start : start + self._batch_size]
            encodings = self._tokenizer.encode_batch([(query, doc) for doc in batch])
            feeds: dict[str, Any] = {}
            ids = np.array([item.ids for item in encodings], dtype=np.int64)
            mask = np.array([item.attention_mask for item in encodings], dtype=np.int64)
            for name in self._input_names:
                if name == "input_ids":
                    feeds[name] = ids
                elif name == "attention_mask":
                    feeds[name] = mask
                elif name == "token_type_ids":
                    feeds[name] = np.zeros_like(ids)
            logits = self._session.run(None, feeds)[0]
            for value in np.asarray(logits).reshape(len(batch), -1)[:, 0]:
                scores.append(_sigmoid(float(value)))
        return scores

    def _document_text(self, hit: SearchHit) -> str:
        content = hit.content[: self._max_doc_chars]
        if not hit.path:
            return content
        header = f"File: {hit.path}\nLines: {hit.start_line}-{hit.end_line}"
        if hit.context:
            header += f"\nContext: {hit.context}"
        return f"{header}\n\n{content}"

    async def close(self) -> None:
        self._session = None
        self._tokenizer = None
