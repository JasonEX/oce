"""Commands, queries, and handlers for model credential administration."""

from __future__ import annotations

from dataclasses import dataclass

from oce.application.messages import Command, Query
from oce.shared.model_credentials import (
    CredentialAdminStore,
    CredentialCreate,
    CredentialPatch,
    CredentialRecord,
)


@dataclass(frozen=True)
class ListCredentialsQuery(Query):
    pass


@dataclass(frozen=True)
class CreateCredentialCommand(Command):
    data: CredentialCreate


@dataclass(frozen=True)
class UpdateCredentialCommand(Command):
    credential_id: int
    changes: CredentialPatch


@dataclass(frozen=True)
class DeleteCredentialCommand(Command):
    credential_id: int


@dataclass(frozen=True)
class DuplicateCredentialCommand(Command):
    credential_id: int
    changes: CredentialPatch


class ListCredentialsQueryHandler:
    def __init__(self, store: CredentialAdminStore) -> None:
        self._store = store

    async def handle(self, _query: ListCredentialsQuery) -> list[CredentialRecord]:
        return await self._store.list()


class CreateCredentialCommandHandler:
    def __init__(self, store: CredentialAdminStore) -> None:
        self._store = store

    async def handle(self, command: CreateCredentialCommand) -> CredentialRecord:
        return await self._store.create(command.data)


class UpdateCredentialCommandHandler:
    def __init__(self, store: CredentialAdminStore) -> None:
        self._store = store

    async def handle(self, command: UpdateCredentialCommand) -> CredentialRecord | None:
        return await self._store.update(command.credential_id, command.changes)


class DeleteCredentialCommandHandler:
    def __init__(self, store: CredentialAdminStore) -> None:
        self._store = store

    async def handle(self, command: DeleteCredentialCommand) -> bool:
        return await self._store.delete(command.credential_id)


class DuplicateCredentialCommandHandler:
    def __init__(self, store: CredentialAdminStore) -> None:
        self._store = store

    async def handle(
        self, command: DuplicateCredentialCommand
    ) -> CredentialRecord | None:
        return await self._store.duplicate(command.credential_id, command.changes)
