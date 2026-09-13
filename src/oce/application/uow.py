"""The unit-of-work protocol the application layer depends on."""

from __future__ import annotations

from types import TracebackType
from typing import Protocol

from oce.domain.repositories import BlobRepository, ChainRepository, ChunkRepository
from oce.domain.services.lexical import LexicalProjection
from oce.domain.services.symbols import SymbolProjection


class UnitOfWork(Protocol):
    # Read-only views: an implementation binds concrete repositories to its
    # transaction, and a use case only ever reads them.
    @property
    def blobs(self) -> BlobRepository: ...

    @property
    def chunks(self) -> ChunkRepository: ...

    @property
    def chains(self) -> ChainRepository: ...

    @property
    def symbols(self) -> SymbolProjection: ...

    @property
    def lexical(self) -> LexicalProjection: ...

    async def __aenter__(self) -> UnitOfWork: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


class UnitOfWorkFactory(Protocol):
    def __call__(self) -> UnitOfWork: ...
