"""容器 token 用量桥接：把 embedder/reranker/llm 的回调映射成 TokenUsageRecord。

只验证纯映射逻辑（credential_id=0 归一 None、total=prompt+completion）；容器用
functools.partial 把 sink 绑定进去，这里直接传 sink。
"""

from __future__ import annotations

from oce.application.container import record_token_usage
from oce.shared.metrics import TokenUsageRecord


class _RecordingSink:
    def __init__(self) -> None:
        self.records: list[TokenUsageRecord] = []

    def record_token_usage(self, record: TokenUsageRecord) -> None:
        self.records.append(record)


async def test_bridge_maps_usage_and_normalizes_zero_credential():
    sink = _RecordingSink()

    # LLM：credential_id=0 → None，total = 12 + 5
    await record_token_usage(sink, 0, "llm", "m", 12, 5)
    rec = sink.records[0]
    assert rec.kind == "llm"
    assert rec.model == "m"
    assert rec.total_tokens == 17
    assert rec.credential_id is None

    # embed：真实凭证 id 透传，completion=0
    await record_token_usage(sink, 7, "embed", "e", 10, 0)
    assert sink.records[1].credential_id == 7
    assert sink.records[1].total_tokens == 10


async def test_bridge_swallows_sink_errors():
    """旁路容错：sink 抛错也不冒泡回主链路。"""

    class _BoomSink:
        def record_token_usage(self, record: TokenUsageRecord) -> None:
            raise RuntimeError("boom")

    # 不抛异常即通过
    await record_token_usage(_BoomSink(), 1, "rerank", "m", 3, 0)
