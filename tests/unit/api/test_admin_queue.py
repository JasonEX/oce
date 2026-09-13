"""Contract tests of the /admin/queue routes."""

from __future__ import annotations

import httpx
from fastapi import Header

from oce.api.router import get_application
from oce.application.commands.queue_admin import ResetQueueResult
from oce.application.commands.requeue import RequeueStaleResult
from oce.application.queries.queue import QueueStatusResult
from oce.auth import _unauthorized, verify_admin_key
from oce.main import app
from oce.shared.errors import QueueBusyError


async def _mock_admin_auth(authorization: str | None = Header(default=None)) -> str:
    if authorization is None or not authorization.startswith("Bearer "):
        raise _unauthorized("missing admin key")
    return authorization.removeprefix("Bearer ")


class StubQueueApp:
    def __init__(self, *, worker_running: bool = False) -> None:
        self.worker_running = worker_running

    async def queue_status(self):
        return QueueStatusResult(enabled=True, main_size=3, inflight=2, db_pending=4)

    async def reset_queue(self, *, mode, requeue):
        if self.worker_running:
            raise QueueBusyError()
        return ResetQueueResult(removed=1, requeued=2, queue_size=2, db_pending=2)

    async def requeue_stale(self, *, stale_hours, limit):
        return RequeueStaleResult(requeued_count=5)


def _client(*, worker_running: bool = False) -> httpx.AsyncClient:
    app.dependency_overrides[get_application] = lambda: StubQueueApp(
        worker_running=worker_running
    )
    app.dependency_overrides[verify_admin_key] = _mock_admin_auth
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


_AUTH = {"Authorization": "Bearer sk-admin"}


async def test_queue_status_contract():
    async with _client() as client:
        response = await client.get("/admin/queue", headers=_AUTH)
    assert response.status_code == 200
    assert response.json() == {
        "enabled": True,
        "main_size": 3,
        "inflight": 2,
        "db_pending": 4,
    }


async def test_queue_reset_ok_when_worker_stopped():
    async with _client(worker_running=False) as client:
        response = await client.post(
            "/admin/queue/reset", headers=_AUTH, json={"mode": "sync"}
        )
    assert response.status_code == 200
    assert response.json()["requeued"] == 2


async def test_queue_reset_conflict_when_worker_running():
    async with _client(worker_running=True) as client:
        response = await client.post(
            "/admin/queue/reset", headers=_AUTH, json={"mode": "purge"}
        )
    assert response.status_code == 409


async def test_requeue_stale_contract():
    async with _client() as client:
        response = await client.post(
            "/admin/queue/requeue-stale",
            headers=_AUTH,
            json={"stale_hours": 12, "limit": 50},
        )
    assert response.status_code == 200
    assert response.json() == {"requeued_count": 5}


async def test_queue_requires_admin_auth():
    async with _client() as client:
        response = await client.get("/admin/queue")
    assert response.status_code == 401
