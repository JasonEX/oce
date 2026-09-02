"""application 异常到 HTTP 状态码的统一映射。

router 只做 DTO 转换；异常语义（ACE 兼容的 400/404/503）在这里集中登记一次。
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from oce.shared.errors import (
    CredentialConflictError,
    InvalidCheckpointTokenError,
    NeedsResetError,
    OCEError,
    QueueBusyError,
    ScopeRequiredError,
    ServiceNotReadyError,
)

_STATUS_BY_ERROR: tuple[tuple[type[OCEError], int], ...] = (
    (ServiceNotReadyError, 503),
    (InvalidCheckpointTokenError, 400),
    (ScopeRequiredError, 400),
    (NeedsResetError, 404),
    (CredentialConflictError, 409),
    (QueueBusyError, 409),
)


def register_error_handlers(app: FastAPI) -> None:
    for error_type, status_code in _STATUS_BY_ERROR:
        app.add_exception_handler(error_type, _handler_for(status_code))


def _handler_for(status_code: int):
    async def handle(_request: Request, exc: Exception) -> JSONResponse:
        headers = {"Retry-After": "0"} if status_code == 503 else None
        return JSONResponse(
            status_code=status_code,
            content={"detail": str(exc)},
            headers=headers,
        )

    return handle
