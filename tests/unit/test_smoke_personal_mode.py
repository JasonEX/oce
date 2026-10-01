"""Personal-mode smoke test: the assembled application answers over HTTP.

The composition root is built exactly as ``oce serve`` builds it, against a
migrated SQLite file, an embedded Milvus Lite file and an OpenAI-compatible
embedding endpoint served in-process. Files are uploaded, indexed and
checkpointed through the application, then retrieved through the FastAPI
router with bearer authentication. A wiring mistake in the container, the
migrations, the vector store adapter or the HTTP layer fails here even when
every unit test passes.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import oce.application.container as container_module
from oce.api.router import get_application
from oce.application.container import Container
from oce.application.service import BlobUpload
from oce.infrastructure.persistence.models import (
    BlobModel,
    ChainModel,
    RetrievalMetricModel,
)
from oce.main import app
from oce.shared.config.settings import (
    EmbeddingSettings,
    MilvusSettings,
    MonitoringSettings,
    RetrievalSettings,
    Settings,
    WorkerSettings,
    get_settings,
)
from oce.shared.errors import ServiceNotReadyError
from oce.shared.model_credentials import CredentialCreate
from tests.fakes.embedding import term_vector
from tests.fakes.queue import FakeQueue

DIMENSIONS = 16
EMBED_API_KEY = "test-embed-key"

FILES = {
    "src/billing/invoice.py": (
        "from src.billing.tax import compute_tax_rate\n"
        "\n"
        "\n"
        "def build_invoice(customer, lines, region):\n"
        "    rate = compute_tax_rate(region)\n"
        "    return {'customer': customer, 'total': sum(lines) * (1 + rate)}\n"
    ),
    "src/billing/tax.py": (
        "RATES = {'eu': 0.2, 'us': 0.07}\n"
        "\n"
        "\n"
        "def compute_tax_rate(region):\n"
        "    if region not in RATES:\n"
        "        raise ValueError('unknown tax region')\n"
        "    return RATES[region]\n"
    ),
    "src/billing/api.py": (
        "from src.billing.invoice import build_invoice\n"
        "\n"
        "\n"
        "def create_invoice(request):\n"
        "    return build_invoice(request.customer, request.lines, request.region)\n"
    ),
    "docs/billing.md": (
        "# Billing\n\nInvoices are built by `build_invoice` from line totals.\n"
    ),
}


@dataclass
class EmbeddingServer:
    url: str
    requests: list[dict[str, object]]


@pytest.fixture
async def embedding_server(unused_tcp_port: int) -> AsyncIterator[EmbeddingServer]:
    """An OpenAI-compatible ``/v1/embeddings`` endpoint on a local port."""
    fake = FastAPI()
    seen: list[dict[str, object]] = []

    @fake.post("/v1/embeddings")
    async def embeddings(request: Request) -> dict[str, object]:
        body = await request.json()
        inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
        seen.append(
            {
                "authorization": request.headers.get("authorization"),
                "model": body.get("model"),
                "count": len(inputs),
            }
        )
        dimensions = int(body.get("dimensions") or DIMENSIONS)
        return {
            "object": "list",
            "model": body.get("model"),
            "data": [
                {
                    "object": "embedding",
                    "index": index,
                    "embedding": term_vector(text, dimensions),
                }
                for index, text in enumerate(inputs)
            ],
            "usage": {"prompt_tokens": len(inputs), "total_tokens": len(inputs)},
        }

    config = uvicorn.Config(
        fake, host="127.0.0.1", port=unused_tcp_port, log_level="error", lifespan="off"
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    try:
        yield EmbeddingServer(f"http://127.0.0.1:{unused_tcp_port}/v1/embeddings", seen)
    finally:
        server.should_exit = True
        await task


@pytest.fixture
async def container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    embedding_server: EmbeddingServer,
    request: pytest.FixtureRequest,
) -> AsyncIterator[Container]:
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'oce.db').as_posix()}"
    # ``oce serve`` migrates the personal database on every start; the
    # migration runner reads the URL from the process settings and drives
    # its own event loop, so it runs in a worker thread here.
    monkeypatch.setenv("DB_URL", db_url)
    get_settings.cache_clear()
    from oce.infrastructure.persistence.migrations import run_migrations

    await asyncio.to_thread(run_migrations)
    engine = create_async_engine(db_url)
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    mode = getattr(request, "param", None)
    has_worker = mode in ("deferred-worker", "disabled-embedding-worker")
    if has_worker:
        queue = FakeQueue()
        monkeypatch.setattr(
            container_module, "_build_redis_queue", lambda _settings: queue
        )
    settings = Settings(
        milvus=MilvusSettings(
            endpoint=str(tmp_path / "milvus.db"),
            # Exact FLAT search keeps assertions over this small fixture
            # independent of approximate-index candidate limits.
            dense_index_type="FLAT",
        ),
        embedding=EmbeddingSettings(
            enabled=mode != "disabled-embedding-worker",
            endpoint=embedding_server.url,
            api_key=None if mode == "deferred-worker" else EMBED_API_KEY,
            model="test-embedding",
            dimensions=DIMENSIONS,
        ),
        retrieval=RetrievalSettings(path_index_enabled=mode is not False),
        worker=WorkerSettings(enabled=has_worker),
        monitoring=MonitoringSettings(
            enabled=True, flush_interval_seconds=60.0, store_query_text=True
        ),
    )
    container = Container(settings, sessions)
    await container.start()
    assert (container.index_lifecycle.current is not None) is (
        mode != "deferred-worker"
    )
    try:
        yield container
    finally:
        await container.close()
        await engine.dispose()
        get_settings.cache_clear()


@pytest.mark.parametrize("container", ["deferred-worker"], indirect=True)
async def test_credentials_reload_starts_deferred_worker_and_allows_queue_reset(
    container: Container, embedding_server: EmbeddingServer
) -> None:
    worker = container.worker
    assert worker is not None and not worker.is_running
    application = container.application
    app.dependency_overrides[get_application] = lambda: application
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://oce"
        ) as client:
            response = await client.post(
                "/batch-upload",
                headers={"Authorization": f"Bearer {get_settings().api_key}"},
                json={
                    "blobs": [
                        {
                            "path": "src/config.py",
                            "content": "def configure(): return 42\n",
                        }
                    ]
                },
            )
        assert response.status_code == 503
        assert "SERVICE_NOT_READY" in response.json()["detail"]
        assert response.headers["Retry-After"] == "0"
    finally:
        app.dependency_overrides.pop(get_application, None)
    with pytest.raises(ServiceNotReadyError, match="Index profile is not ready"):
        await application.checkpoint(
            checkpoint_id=None, added_blobs=[], deleted_blobs=[]
        )
    async with container.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(BlobModel)) == 0
        assert await session.scalar(select(func.count()).select_from(ChainModel)) == 0
    assert not embedding_server.requests
    missing_credentials = await application.reload_embedding_credentials()
    assert not missing_credentials.reloaded
    assert not worker.is_running

    await application.create_credential(
        CredentialCreate(
            kind="embed",
            name="local test endpoint",
            api_key=EMBED_API_KEY,
            endpoint=embedding_server.url,
            model="test-embedding",
            dimensions=DIMENSIONS,
        )
    )
    assert not worker.is_running
    assert (await application.reload_embedding_credentials()).reloaded
    assert worker.is_running

    uploaded = await application.batch_upload(
        [BlobUpload("src/config.py", "def configure(): return 42\n")]
    )
    assert uploaded.embedded_count == 0

    async def wait_ready() -> None:
        while (await application.find_missing(list(uploaded.blob_names))).nonindexed:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait_ready(), timeout=5)
    assert embedding_server.requests
    reset = await application.reset_queue()
    assert reset.db_pending == 0 and reset.queue_size == 0
    assert worker.is_running


@pytest.mark.parametrize("container", ["disabled-embedding-worker"], indirect=True)
async def test_disabled_embedding_still_chunks_service_mode_uploads(
    container: Container, embedding_server: EmbeddingServer
) -> None:
    assert (await container.application.reload_embedding_credentials()).reloaded
    await container.start_worker()
    assert container.worker is not None and container.worker.is_running
    uploaded = await container.application.batch_upload(
        [BlobUpload("src/config.py", "def configure(): return 42\n")]
    )
    queue = container.queue
    assert isinstance(queue, FakeQueue)

    async def wait_ack() -> None:
        while uploaded.blob_names[0] not in queue.acked:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait_ack(), timeout=5)
    async with container.indexing.uow_factory() as uow:
        blob = await uow.blobs.get(uploaded.blob_names[0])
        assert blob is not None and blob.chunks
        assert blob.status.value == "pending"
        assert await uow.blobs.get_staging(blob.blob_name) is not None
    assert not embedding_server.requests


async def test_upload_checkpoint_and_retrieve_through_http(
    container: Container, embedding_server: EmbeddingServer
) -> None:
    application = container.application
    uploaded = await application.batch_upload(
        [BlobUpload(path, content) for path, content in FILES.items()]
    )
    assert len(uploaded.blob_names) == len(FILES)
    assert uploaded.embedded_count > 0
    # The credential fell back to EMBED_API_KEY and the client sent it.
    assert embedding_server.requests
    assert all(
        request["authorization"] == f"Bearer {EMBED_API_KEY}"
        for request in embedding_server.requests
    )

    checkpoint = await application.checkpoint(
        checkpoint_id=None, added_blobs=list(uploaded.blob_names), deleted_blobs=[]
    )
    missing = await application.find_missing(list(uploaded.blob_names))
    assert missing.unknown == () and missing.nonindexed == ()

    api_key = get_settings().api_key
    app.dependency_overrides[get_application] = lambda: application
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://oce"
        ) as client:
            headers = {"Authorization": f"Bearer {api_key}"}

            unauthorized = await client.post(
                "/agents/codebase-retrieval",
                json={"information_request": "x", "blobs": {}},
            )
            assert unauthorized.status_code == 401

            symbol = await client.post(
                "/agents/codebase-retrieval",
                headers=headers,
                json={
                    "information_request": "Where is `build_invoice` defined?",
                    "blobs": {"checkpoint_id": checkpoint.new_checkpoint_id},
                },
            )
            assert symbol.status_code == 200, symbol.text
            formatted = symbol.json()["formatted_retrieval"]
            assert formatted.startswith("The following code sections were retrieved:")
            # The declaring file leads; the documentation that quotes the
            # name never takes a focused symbol answer's first slot.
            paths = [
                line.removeprefix("Path: ")
                for line in formatted.splitlines()
                if line.startswith("Path: ")
            ]
            assert paths[0] == "src/billing/invoice.py"
            assert "def build_invoice" in formatted

            reference = await client.post(
                "/agents/codebase-retrieval",
                headers=headers,
                json={
                    "information_request": "Where is `compute_tax_rate` used?",
                    "blobs": {"added_blobs": list(uploaded.blob_names)},
                },
            )
            assert reference.status_code == 200, reference.text
            assert "src/billing/invoice.py" in reference.json()["formatted_retrieval"]

            semantic = await client.post(
                "/agents/codebase-retrieval",
                headers=headers,
                json={
                    "information_request": "How is the tax rate applied to an invoice?",
                    "blobs": {"checkpoint_id": checkpoint.new_checkpoint_id},
                },
            )
            assert semantic.status_code == 200, semantic.text
            assert "Path: src/billing/" in semantic.json()["formatted_retrieval"]

            stats = await client.get("/admin/index-stats", headers=headers)
            assert stats.status_code == 200, stats.text
            payload = stats.json()
            assert payload["metadata"]["blobs_ready"] == len(FILES)
            assert payload["dense"]["available"] is True
            assert payload["profile"]["state"] == "compatible"
    finally:
        app.dependency_overrides.pop(get_application, None)

    # Monitoring is a side channel, but a request that silently answered
    # from fewer lanes must be visible to whoever reads the metrics.
    await container.monitoring.metrics.stop()
    async with container.session_factory() as session:
        rows = (
            await session.execute(
                select(
                    RetrievalMetricModel.intent,
                    RetrievalMetricModel.dense_route,
                    RetrievalMetricModel.lane_failures,
                    RetrievalMetricModel.hit_count,
                ).order_by(RetrievalMetricModel.id)
            )
        ).all()
        total = await session.scalar(
            select(func.count()).select_from(RetrievalMetricModel)
        )
    assert total == 3
    assert [row.intent for row in rows] == ["symbol", "reference", "feature"]
    assert all(row.lane_failures is None for row in rows)
    assert all(row.hit_count > 0 for row in rows)
    assert rows[0].dense_route == "skip:exact_definition"
    assert rows[2].dense_route == "dense"


@pytest.mark.parametrize("container", [False], indirect=True)
async def test_sql_path_retrieval_without_vector_path_index(
    container: Container,
) -> None:
    uploaded = await container.application.batch_upload(
        [BlobUpload("src/settings.cfg", "timeout = 42\n")]
    )
    assert container.stores.path_index is None
    assert container.stores.path_content_store is not None

    app.dependency_overrides[get_application] = lambda: container.application
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://oce"
        ) as client:
            response = await client.post(
                "/agents/codebase-retrieval",
                headers={"Authorization": f"Bearer {get_settings().api_key}"},
                json={
                    "information_request": "Where is settings.cfg?",
                    "blobs": {"added_blobs": list(uploaded.blob_names)},
                },
            )
            assert response.status_code == 200, response.text
            text = response.json()["formatted_retrieval"]
            assert "Path: src/settings.cfg" in text
            assert "timeout = 42" in text
    finally:
        app.dependency_overrides.pop(get_application, None)
