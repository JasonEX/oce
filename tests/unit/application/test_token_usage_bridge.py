"""The container's usage bridge maps model-client callbacks to TokenUsageRecord.

Only the mapping is tested (credential_id 0 becomes None, total is prompt
plus completion); the container binds the sink with functools.partial, here
it is passed directly.
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

    # embed: the credential id passes through, completion is 0
    await record_token_usage(sink, 7, "embed", "e", 10, 0)
    assert sink.records[1].credential_id == 7
    assert sink.records[1].total_tokens == 10


async def test_bridge_swallows_sink_errors():
    """A raising sink never propagates to the caller."""

    class _BoomSink:
        def record_token_usage(self, record: TokenUsageRecord) -> None:
            raise RuntimeError("boom")

    # passing means no exception
    await record_token_usage(_BoomSink(), 1, "rerank", "m", 3, 0)
