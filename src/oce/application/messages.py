"""Marker base classes for commands (writes) and queries (reads).

Concrete messages are frozen dataclasses; the composition root registers
their handlers on the buses.
"""

from __future__ import annotations


class Command:
    """An immutable input to a write use case."""


class Query:
    """An immutable input to a read use case."""
