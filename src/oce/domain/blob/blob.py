"""Blob 聚合根 - 文件抽象

Blob 是文件的领域抽象，职责：
- 管理文件元数据（path/status/last_seen）
- 管理关联的 Chunk 列表
- 状态转换（pending -> ready/error）

不变量：
- blob_name 必须是有效的 SHA256
- status 状态机：pending -> ready/error
- 空文本或二进制文件可以没有 chunk 并直接标记 ready
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING

from oce.shared.hashes import is_sha256_hex

if TYPE_CHECKING:
    from oce.domain.chunk import ChunkRef


class BlobStatus(str, Enum):
    """Blob 状态枚举"""

    PENDING = "pending"  # 等待嵌入
    READY = "ready"  # 已就绪
    ERROR = "error"  # 嵌入失败


@dataclass
class Blob:
    """Blob 聚合根"""

    blob_name: str  # PK - SHA256(path + content)
    path: str  # 文件相对路径
    status: BlobStatus = BlobStatus.PENDING
    chunks: list[ChunkRef] = field(default_factory=list)
    content_size: int = 0
    language: str | None = None
    file_type: str = "text"
    retry_count: int = 0  # 失败重试次数
    last_seen: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    error_message: str | None = None  # 失败原因

    def __post_init__(self):
        """验证不变量"""
        if not is_sha256_hex(self.blob_name):
            raise ValueError(f"Invalid blob_name (not SHA256): {self.blob_name}")

    def mark_ready(self) -> None:
        """标记为就绪；空文本文件也可以完成索引。"""
        self.status = BlobStatus.READY
        self.error_message = None

    def mark_error(self, error_message: str) -> None:
        """标记为错误状态"""
        self.status = BlobStatus.ERROR
        self.error_message = error_message

    def increment_retry(self, max_retries: int = 3) -> bool:
        """增加重试计数，超限自动 mark_error。返回是否已超限。"""
        self.retry_count += 1
        if self.retry_count >= max_retries:
            self.mark_error(f"Failed after {self.retry_count} retries")
            return True
        return False

    def touch(self) -> None:
        """更新最后访问时间"""
        self.last_seen = datetime.now(timezone.utc)

    def is_ready(self) -> bool:
        return self.status == BlobStatus.READY

    def __repr__(self) -> str:
        return (
            f"Blob(name={self.blob_name[:8]}..., path={self.path}, "
            f"status={self.status.value}, chunks={len(self.chunks)})"
        )
