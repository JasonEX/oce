"""Typed use-case doubles for testing application orchestration."""

from dataclasses import fields
from typing import get_type_hints
from unittest.mock import create_autospec

from oce.application.commands.ingest import EmbedPendingResult
from oce.application.queries.search import SearchResult
from oce.application.queries.status import ResolveScopeResult
from oce.application.use_cases import ApplicationCommands, ApplicationQueries
from oce.domain.services.search import SearchHit, SearchScope
from oce.shared.model_credentials import CredentialAdminStore


def mocked_use_cases() -> tuple[
    ApplicationCommands, ApplicationQueries, CredentialAdminStore
]:
    commands = ApplicationCommands(
        **{
            item.name: create_autospec(
                get_type_hints(ApplicationCommands)[item.name], instance=True
            )
            for item in fields(ApplicationCommands)
        }
    )
    queries = ApplicationQueries(
        **{
            item.name: create_autospec(
                get_type_hints(ApplicationQueries)[item.name], instance=True
            )
            for item in fields(ApplicationQueries)
        }
    )
    commands.embed_pending.handle.return_value = EmbedPendingResult(0)
    queries.resolve_scope.handle.return_value = ResolveScopeResult(
        SearchScope(frozenset({"blob-a"}))
    )
    queries.search.handle.return_value = SearchResult(
        [SearchHit("blob-a", "src/a.py", "def a(): pass", 0.9)]
    )
    return commands, queries, create_autospec(CredentialAdminStore, instance=True)
