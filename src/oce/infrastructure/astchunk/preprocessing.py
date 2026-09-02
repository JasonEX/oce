"""Non-whitespace budgets over byte ranges of a source file."""

from __future__ import annotations

import string
from dataclasses import dataclass

import numpy as np

_WHITESPACE_BYTES = frozenset(string.whitespace.encode("ascii"))


@dataclass(frozen=True, order=True)
class ByteRange:
    """Half-open byte range ``[start, stop)`` inside one source file."""

    start: int
    stop: int

    def __post_init__(self) -> None:
        if self.stop < self.start:
            raise ValueError(f"A valid range must have {self.start=} <= {self.stop=}.")


def preprocess_nws_count(source: bytes) -> np.ndarray:
    """Prefix sums of non-whitespace bytes, so any range costs O(1) to measure."""
    is_nws = np.fromiter(
        (byte not in _WHITESPACE_BYTES for byte in source),
        dtype=bool,
        count=len(source),
    )
    return np.concatenate(([0], np.cumsum(is_nws)))


def get_nws_count(nws_cumsum: np.ndarray, brange: ByteRange) -> int:
    return int(nws_cumsum[brange.stop] - nws_cumsum[brange.start])
