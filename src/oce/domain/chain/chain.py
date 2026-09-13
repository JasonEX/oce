"""The Chain aggregate: a client's working set.

A chain is a set of blob members plus a monotonically increasing checkpoint
version. Membership changes and version bumps happen in one transaction in
the repository; this class holds the read model and the checkpoint token
encoding. ``chain_id`` must be a UUID and ``version`` at least 1.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Chain:
    chain_id: str  # UUID
    version: int  # starts at 1
    members: set[str] = field(default_factory=set)  # blob names
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if not self._is_valid_uuid(self.chain_id):
            raise ValueError(f"Invalid chain_id (not UUID): {self.chain_id}")
        if self.version < 1:
            raise ValueError(f"Invalid version (must be >= 1): {self.version}")

    @staticmethod
    def _is_valid_uuid(s: str) -> bool:
        try:
            uuid.UUID(s, version=4)
            return True
        except (ValueError, AttributeError):
            return False

    @staticmethod
    def format_checkpoint_token(chain_id: str, version: int) -> str:
        """``{chain_id}:{version}``, an opaque token the client stores and returns.

        A version mismatch makes the server demand a rebuilt working set, so
        a stale token can neither read nor rewrite a newer member set.
        """
        return f"{chain_id}:{version}"

    @staticmethod
    def parse_checkpoint_token(token: str) -> tuple[str, int] | None:
        """``(chain_id, version)``, or None when the token is malformed."""
        if not token or ":" not in token:
            return None

        chain_id, _, version_str = token.rpartition(":")
        if not chain_id or not version_str.isdigit():
            return None

        if not Chain._is_valid_uuid(chain_id):
            return None

        return chain_id, int(version_str)

    def __repr__(self) -> str:
        return (
            f"Chain(id={self.chain_id[:8]}..., "
            f"version={self.version}, members={len(self.members)})"
        )
