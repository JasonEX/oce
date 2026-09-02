"""Chain 聚合根 - 工作集抽象

Chain 是客户端工作集的领域抽象：一组 Blob 成员加一个单调递增的 checkpoint
版本。成员增删与版本推进由 ChainRepository 在同一事务内完成，这里只承载读模型
和 checkpoint 令牌的编解码。

不变量：
- chain_id 必须是有效的 UUID
- version 必须 >= 1
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Chain:
    """Chain 聚合根 - 工作集"""

    chain_id: str  # UUID
    version: int  # 版本号（从 1 开始）
    members: set[str] = field(default_factory=set)  # Blob 成员集合
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        """验证不变量"""
        if not self._is_valid_uuid(self.chain_id):
            raise ValueError(f"Invalid chain_id (not UUID): {self.chain_id}")
        if self.version < 1:
            raise ValueError(f"Invalid version (must be >= 1): {self.version}")

    @staticmethod
    def _is_valid_uuid(s: str) -> bool:
        """验证 UUID 格式"""
        try:
            uuid.UUID(s, version=4)
            return True
        except (ValueError, AttributeError):
            return False

    @staticmethod
    def format_checkpoint_token(chain_id: str, version: int) -> str:
        """格式：{chain_id}:{version}

        不透明令牌，客户端必须存储并回传最新值。版本不匹配时服务端要求
        重建工作集，避免旧令牌静默读取或改写新成员集。
        """
        return f"{chain_id}:{version}"

    @staticmethod
    def parse_checkpoint_token(token: str) -> tuple[str, int] | None:
        """解析 Checkpoint 令牌

        返回：(chain_id, version) 或 None（格式非法）
        """
        if not token or ":" not in token:
            return None

        chain_id, _, version_str = token.rpartition(":")
        if not chain_id or not version_str.isdigit():
            return None

        # 验证 UUID 格式
        if not Chain._is_valid_uuid(chain_id):
            return None

        return chain_id, int(version_str)

    def __repr__(self) -> str:
        return (
            f"Chain(id={self.chain_id[:8]}..., "
            f"version={self.version}, members={len(self.members)})"
        )
