"""应用层 Command / Query 标记基类。

CQRS 约定：
- Command（命令）：写操作，改变系统状态，经 CommandBus.execute 分发
- Query（查询）：读操作，不改变状态，经 QueryBus.ask 分发

具体消息一律用 frozen dataclass 定义，由 composition root 注册处理器。
"""

from __future__ import annotations


class Command:
    """命令标记基类（写操作）"""


class Query:
    """查询标记基类（读操作）"""
