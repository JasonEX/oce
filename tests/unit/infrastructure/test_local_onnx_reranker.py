"""In-process ONNX reranker: window, ordering, candidate preservation, failure."""

from __future__ import annotations

import numpy as np

from oce.domain.services.search import SearchHit
from oce.infrastructure.embed.local_onnx_reranker import LocalOnnxReranker, _sigmoid


def _hit(path: str, score: float, content: str = "code") -> SearchHit:
    return SearchHit(blob_name="a" * 64, path=path, content=content, score=score)


class _Encoding:
    def __init__(self, ids: list[int]):
        self.ids = ids
        self.attention_mask = [1] * len(ids)


class _Tokenizer:
    def encode_batch(self, pairs):
        return [_Encoding([1, 2, 3]) for _ in pairs]


class _Session:
    """Scores a document by how many times 'pool' occurs in it."""

    def __init__(self):
        self.batches: list[int] = []
        self.documents: list[str] = []

    def run(self, _outputs, feeds):
        batch = feeds["input_ids"].shape[0]
        self.batches.append(batch)
        start = len(self.documents) - batch
        docs = self.documents[start:] if start >= 0 else self.documents
        return [np.array([[float(doc.count("pool"))] for doc in docs])]


def _loaded(candidates=20, batch_size=4) -> tuple[LocalOnnxReranker, _Session]:
    reranker = LocalOnnxReranker(
        model_dir="/nonexistent", candidates=candidates, batch_size=batch_size
    )
    session = _Session()
    tokenizer = _Tokenizer()

    original = tokenizer.encode_batch

    def recording(pairs):
        session.documents.extend(doc for _query, doc in pairs)
        return original(pairs)

    tokenizer.encode_batch = recording
    reranker._session = session
    reranker._tokenizer = tokenizer
    reranker._input_names = ("input_ids", "attention_mask")
    return reranker, session


async def test_window_is_reordered_and_tail_is_preserved():
    reranker, session = _loaded(candidates=3, batch_size=2)
    hits = [
        _hit("a.py", 0.9, "nothing"),
        _hit("b.py", 0.8, "pool pool pool"),
        _hit("c.py", 0.7, "pool"),
        _hit("d.py", 0.6, "pool pool pool pool"),
    ]
    result = await reranker.rerank("pool", hits)
    # Only the first three are scored; d.py stays behind them untouched.
    assert [hit.path for hit in result] == ["b.py", "c.py", "a.py", "d.py"]
    assert result[3].score == 0.6
    assert session.batches == [2, 1]
    assert all(0.0 <= hit.score <= 1.0 for hit in result[:3])


async def test_document_text_carries_path_context_and_is_capped():
    reranker, session = _loaded()
    reranker._max_doc_chars = 10
    hit = SearchHit(
        blob_name="a" * 64,
        path="src/x.py",
        content="x" * 100,
        score=0.5,
        start_line=3,
        end_line=9,
        context="class Foo",
    )
    await reranker.rerank("q", [hit])
    assert (
        session.documents[0]
        == "File: src/x.py\nLines: 3-9\nContext: class Foo\n\n" + "x" * 10
    )


async def test_missing_runtime_keeps_retrieval_order():
    reranker = LocalOnnxReranker(model_dir="/definitely/missing")
    hits = [_hit("a.py", 0.9), _hit("b.py", 0.8)]
    assert await reranker.rerank("q", hits) == hits
    assert reranker._load_failed is True
    # A static configuration error is not retried and logged for every query.
    assert await reranker.rerank("q", hits) == hits


def test_sigmoid_handles_extreme_logits_without_overflow():
    assert _sigmoid(-1_000.0) == 0.0
    assert _sigmoid(1_000.0) == 1.0
