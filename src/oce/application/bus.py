"""应用层消息总线 - CommandBus / QueryBus

简单注册分发：消息类型 → handler 的 dict 映射。
- CommandBus.execute: 执行写命令（可批量）
- QueryBus.ask:      执行读查询

未注册的消息抛 ApplicationError 系异常（COMMAND_NOT_REGISTERED /
QUERY_NOT_REGISTERED），由 API 层统一转 HTTP 500。
"""

from __future__ import annotations

from typing import Any

from oce.shared.errors import ApplicationError


class CommandNotRegisteredError(ApplicationError):
    """命令未注册处理器"""

    def __init__(self, command_type: type) -> None:
        super().__init__(
            f"No handler registered for command: {command_type.__name__}",
            code="COMMAND_NOT_REGISTERED",
        )


class QueryNotRegisteredError(ApplicationError):
    """查询未注册处理器"""

    def __init__(self, query_type: type) -> None:
        super().__init__(
            f"No handler registered for query: {query_type.__name__}",
            code="QUERY_NOT_REGISTERED",
        )


class _MessageBus:
    """消息类型 → handler 的注册分发；命令与查询只在未注册异常上不同。"""

    _not_registered: type[ApplicationError]

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
    """命令总线"""

    _not_registered = CommandNotRegisteredError
    execute = _MessageBus.dispatch


class QueryBus(_MessageBus):
    """查询总线"""

    _not_registered = QueryNotRegisteredError
    ask = _MessageBus.dispatch
