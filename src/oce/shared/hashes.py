"""Content-address validation shared by domain values and store filters."""

from __future__ import annotations

_HEX_DIGITS = frozenset("0123456789abcdef")


def is_sha256_hex(value: str) -> bool:
    """Whether ``value`` is a 64-character hexadecimal SHA256 digest."""
    return len(value) == 64 and set(value.lower()) <= _HEX_DIGITS
