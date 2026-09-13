"""Checkpoint (working set) command."""

from __future__ import annotations

from dataclasses import dataclass

from oce.application.messages import Command
from oce.application.uow import UnitOfWorkFactory
from oce.domain.chain.chain import Chain
from oce.shared.errors import InvalidCheckpointTokenError, NeedsResetError


@dataclass(frozen=True)
class CheckpointCommand(Command):
    checkpoint_id: str | None = None
    added_blobs: tuple[str, ...] = ()
    deleted_blobs: tuple[str, ...] = ()


@dataclass(frozen=True)
class CheckpointResult:
    new_checkpoint_id: str


class CheckpointCommandHandler:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def handle(self, command: CheckpointCommand) -> CheckpointResult:
        async with self._uow_factory() as uow:
            if not command.checkpoint_id:
                members = sorted(set(command.added_blobs) - set(command.deleted_blobs))
                chain = await uow.chains.create(members)
                chain_id, version = chain.chain_id, chain.version
            else:
                parsed = Chain.parse_checkpoint_token(command.checkpoint_id)
                if parsed is None:
                    raise InvalidCheckpointTokenError(command.checkpoint_id)
                chain_id, expected_version = parsed
                applied = await uow.chains.apply_checkpoint(
                    chain_id,
                    expected_version,
                    command.added_blobs,
                    command.deleted_blobs,
                )
                if applied is None:
                    raise NeedsResetError(
                        "checkpoint chain not found or version outdated"
                    )
                version = applied
            await uow.chains.touch_members(chain_id)
            await uow.commit()
        return CheckpointResult(Chain.format_checkpoint_token(chain_id, version))
