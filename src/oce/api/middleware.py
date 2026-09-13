"""HTTP call monitoring middleware.

Every request reports its route template, method, status code and latency to
the metrics sink; ``exempt_paths`` (``/health`` by default) are not recorded.
Collection is a side channel: a failure is logged and never touches the
request, and a ``sink_provider`` that returns None (the container is not
assembled yet) skips recording rather than building the container.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from time import perf_counter

from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from oce.shared.metrics import ApiCallRecord, MetricsSink

_ENDPOINT_MAX = 128

SinkProvider = Callable[[], MetricsSink | None]


class ApiCallMetricsMiddleware(BaseHTTPMiddleware):
    """Record each request's latency and status without changing the response."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        sink_provider: SinkProvider,
        exempt_paths: frozenset[str] = frozenset({"/health"}),
    ) -> None:
        super().__init__(app)
        self._sink_provider = sink_provider
        self._exempt = exempt_paths

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.url.path in self._exempt:
            return await call_next(request)

        started = perf_counter()
        status_code = 500  # what an uncaught exception becomes
        error_type: str | None = None
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        except Exception as exc:  # recorded, then re-raised for the error handlers
            error_type = type(exc).__name__
            raise
        finally:
            self._record(request, status_code, error_type, perf_counter() - started)

    def _record(
        self,
        request: Request,
        status_code: int,
        error_type: str | None,
        elapsed_s: float,
    ) -> None:
        try:
            sink = self._sink_provider()
            if sink is None:
                return
            route = request.scope.get("route")
            endpoint = getattr(route, "path", None) or request.url.path
            sink.record_api_call(
                ApiCallRecord(
                    endpoint=endpoint[:_ENDPOINT_MAX],
                    method=request.method,
                    status_code=status_code,
                    latency_ms=int(elapsed_s * 1000),
                    error_type=error_type,
                )
            )
        except Exception as exc:  # monitoring never affects the request
            logger.warning("record api call failed: {}", exc)
