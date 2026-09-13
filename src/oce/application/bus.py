"""Command and query buses: a message-type to handler registry.

``CommandBus.execute`` runs a write command, ``QueryBus.ask`` a read query.
An unregistered message raises an ``ApplicationError`` (COMMAND_NOT_REGISTERED
or QUERY_NOT_REGISTERED) that the API layer maps to HTTP 500.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from oce.shared.errors import ApplicationError


class CommandNotRegisteredError(ApplicationError):
    def __init__(self, command_type: type) -> None:
        super().__init__(
            f"No handler registered for command: {command_type.__name__}",
            code="COMMAND_NOT_REGISTERED",
        )


class QueryNotRegisteredError(ApplicationError):
    def __init__(self, query_type: type) -> None:
        super().__init__(
            f"No handler registered for query: {query_type.__name__}",
            code="QUERY_NOT_REGISTERED",
        )


class _MessageBus:
    """Dispatch by message type; commands and queries differ only in the unregistered error."""

    _not_registered: Callable[[type], ApplicationError]

    def __init__(self) -> None:
        self._handlers: dict[type, Any] = {}

    def register(self, message_type: type, handler: Any) -> None:
        self._handlers[message_type] = handler

    async def dispatch(self, message: Any) -> Any:
        handler = self._handlers.get(type(message))
        if handler is None:
            raise self._not_registered(type(message))
        return await handler.handle(message)


class CommandBus(_MessageBus):
    _not_registered = CommandNotRegisteredError
    execute = _MessageBus.dispatch


class QueryBus(_MessageBus):
    _not_registered = QueryNotRegisteredError
    ask = _MessageBus.dispatch
