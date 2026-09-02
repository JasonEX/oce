"""Lazily built delegate that can be swapped while calls are in flight.

Credential hot-reload replaces the underlying HTTP client. A replacement must
not close the previous client under a call that is still using it, so calls
are counted per delegate and a retired delegate is closed by its last caller.
"""

from __future__ import annotations

import asyncio
from typing import Generic, TypeVar

T = TypeVar("T")


class SwappableDelegate(Generic[T]):
    def __init__(self) -> None:
        self._delegate: T | None = None
        self._lock = asyncio.Lock()
        self._active_calls: dict[int, int] = {}
        self._retired: dict[int, T] = {}

    async def _create_delegate(self) -> T:
        """Build the first delegate on demand; subclasses resolve their config here."""
        raise NotImplementedError

    async def _acquire(self) -> T:
        async with self._lock:
            if self._delegate is None:
                self._delegate = await self._create_delegate()
            delegate = self._delegate
            key = id(delegate)
            self._active_calls[key] = self._active_calls.get(key, 0) + 1
            return delegate

    async def _release(self, delegate: T) -> None:
        close_delegate = False
        key = id(delegate)
        async with self._lock:
            remaining = self._active_calls[key] - 1
            if remaining:
                self._active_calls[key] = remaining
            else:
                del self._active_calls[key]
                close_delegate = self._retired.pop(key, None) is not None
        if close_delegate:
            await self._close_delegate(delegate)

    async def _activate(self, replacement: T) -> None:
        """Swap in ``replacement``; the previous delegate closes once idle."""
        close_previous: T | None = None
        async with self._lock:
            previous = self._delegate
            self._delegate = replacement
            if previous is not None:
                if self._active_calls.get(id(previous), 0):
                    self._retired[id(previous)] = previous
                else:
                    close_previous = previous
        if close_previous is not None:
            await self._close_delegate(close_previous)

    async def close(self) -> None:
        async with self._lock:
            delegates = list(self._retired.values())
            if self._delegate is not None:
                delegates.append(self._delegate)
            self._delegate = None
            self._retired.clear()
        await asyncio.gather(*(self._close_delegate(item) for item in delegates))

    @staticmethod
    async def _close_delegate(delegate: T) -> None:
        close = getattr(delegate, "close", None)
        if close is not None:
            await close()
