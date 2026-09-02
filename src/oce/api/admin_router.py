"""Admin 运维路由：独立鉴权（verify_admin_key），与 agent 数据面分离。

application 异常（凭据冲突 409、队列忙 409 等）由 api/errors.py 统一映射。
"""

from __future__ import annotations

from typing import TypeVar

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from oce.api.router import get_application
from oce.api.schemas import (
    CredentialCreateRequest,
    CredentialListResponse,
    CredentialPatchRequest,
    CredentialResponse,
    GcRequest,
    GcResponse,
    IndexStatsResponse,
    MonitoringStatsResponse,
    QueueResetRequest,
    QueueResetResponse,
    QueueStatusResponse,
    ReloadCredentialsResponse,
    RequeueStaleRequest,
    RequeueStaleResponse,
)
from oce.application.service import RetrievalApplication
from oce.auth import verify_admin_key
from oce.shared.model_credentials import (
    CredentialCreate,
    CredentialPatch,
    CredentialRecord,
)

admin_router = APIRouter(
    prefix="/admin",
    tags=["Admin"],
    dependencies=[Depends(verify_admin_key)],
)


def _credential_response(record: CredentialRecord) -> CredentialResponse:
    # CredentialResponse 字段与 CredentialRecord 同名（不含明文 api_key），按属性直接映射。
    return CredentialResponse.model_validate(record, from_attributes=True)


_ResponseT = TypeVar("_ResponseT", bound=BaseModel)


def _response(model: type[_ResponseT], result: object) -> _ResponseT:
    """application 结果 dataclass 与响应 DTO 同名字段，按属性映射。"""
    return model.model_validate(result, from_attributes=True)


@admin_router.get("/credentials", response_model=CredentialListResponse)
async def list_credentials(
    application: RetrievalApplication = Depends(get_application),
) -> CredentialListResponse:
    records = await application.list_credentials()
    return CredentialListResponse(
        credentials=[_credential_response(record) for record in records]
    )


@admin_router.post("/credentials", response_model=CredentialResponse, status_code=201)
async def create_credential(
    request: CredentialCreateRequest,
    application: RetrievalApplication = Depends(get_application),
) -> CredentialResponse:
    record = await application.create_credential(
        CredentialCreate(**request.model_dump())
    )
    return _credential_response(record)


@admin_router.patch("/credentials/{credential_id}", response_model=CredentialResponse)
async def update_credential(
    credential_id: int,
    request: CredentialPatchRequest,
    application: RetrievalApplication = Depends(get_application),
) -> CredentialResponse:
    record = await application.update_credential(
        credential_id, CredentialPatch(**request.model_dump())
    )
    if record is None:
        raise HTTPException(status_code=404, detail="credential not found")
    return _credential_response(record)


@admin_router.delete("/credentials/{credential_id}", status_code=204)
async def delete_credential(
    credential_id: int,
    application: RetrievalApplication = Depends(get_application),
) -> None:
    deleted = await application.delete_credential(credential_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="credential not found")


@admin_router.post(
    "/credentials/{credential_id}/duplicate",
    response_model=CredentialResponse,
    status_code=201,
)
async def duplicate_credential(
    credential_id: int,
    request: CredentialPatchRequest,
    application: RetrievalApplication = Depends(get_application),
) -> CredentialResponse:
    record = await application.duplicate_credential(
        credential_id, CredentialPatch(**request.model_dump())
    )
    if record is None:
        raise HTTPException(status_code=404, detail="credential not found")
    return _credential_response(record)


@admin_router.post("/credentials/reload", response_model=ReloadCredentialsResponse)
async def reload_credentials(
    application: RetrievalApplication = Depends(get_application),
) -> ReloadCredentialsResponse:
    result = await application.reload_embedding_credentials()
    return _response(ReloadCredentialsResponse, result)


@admin_router.get("/queue", response_model=QueueStatusResponse)
async def queue_status(
    application: RetrievalApplication = Depends(get_application),
) -> QueueStatusResponse:
    return _response(QueueStatusResponse, await application.queue_status())


@admin_router.post("/queue/reset", response_model=QueueResetResponse)
async def reset_queue(
    request: QueueResetRequest,
    application: RetrievalApplication = Depends(get_application),
) -> QueueResetResponse:
    result = await application.reset_queue(mode=request.mode, requeue=request.requeue)
    return _response(QueueResetResponse, result)


@admin_router.post("/queue/requeue-stale", response_model=RequeueStaleResponse)
async def requeue_stale(
    request: RequeueStaleRequest,
    application: RetrievalApplication = Depends(get_application),
) -> RequeueStaleResponse:
    result = await application.requeue_stale(
        stale_hours=request.stale_hours, limit=request.limit
    )
    return _response(RequeueStaleResponse, result)


@admin_router.post("/gc", response_model=GcResponse)
async def run_gc(
    request: GcRequest,
    application: RetrievalApplication = Depends(get_application),
) -> GcResponse:
    result = await application.run_gc(
        ttl_days=request.ttl_days, dry_run=request.dry_run, limit=request.limit
    )
    return _response(GcResponse, result)


@admin_router.get("/stats", response_model=MonitoringStatsResponse)
async def admin_stats(
    window_hours: int = 24,
    application: RetrievalApplication = Depends(get_application),
) -> MonitoringStatsResponse:
    stats = await application.monitoring_stats(window_hours=window_hours)
    return _response(MonitoringStatsResponse, stats)


@admin_router.get("/index-stats", response_model=IndexStatsResponse)
async def admin_index_stats(
    application: RetrievalApplication = Depends(get_application),
) -> IndexStatsResponse:
    return _response(IndexStatsResponse, await application.index_stats())
