"""Marker base classes for commands (writes) and queries (reads).

Concrete messages are frozen dataclasses; the composition root registers
their handlers on the buses.
"""

from __future__ import annotations


class Command:
    """A write operation dispatched through ``CommandBus.execute``."""


class Query:
    """A read operation dispatched through ``QueryBus.ask``."""
