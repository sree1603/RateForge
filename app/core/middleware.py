import logging
import re
import time
import uuid
from datetime import UTC, datetime

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.logging import request_id_ctx
from app.core.usage_context import UsageContext, UsageRecord, usage_ctx
from app.observability import metrics

logger = logging.getLogger("rateforge.request")

REQUEST_ID_HEADER = "X-Request-ID"
# An inbound ID is echoed in response headers, logs and upstream requests, so it must be
# boring: no spaces, control characters or anything that could split a header line.
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class RequestContextMiddleware:
    """Assigns a request ID, echoes it in the response, and logs one line per request.

    Written as pure ASGI rather than @app.middleware("http") so that streamed proxy
    responses pass through without being buffered or re-wrapped.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Reuse an inbound ID only if it is safe; otherwise generate our own.
        inbound = Headers(scope=scope).get(REQUEST_ID_HEADER, "")
        request_id = inbound if _SAFE_REQUEST_ID.match(inbound) else f"req_{uuid.uuid4().hex[:16]}"
        token = request_id_ctx.set(request_id)
        usage = UsageContext()
        usage_token = usage_ctx.set(usage)
        started_at = datetime.now(UTC)
        start = time.perf_counter()
        status_code = 500
        metrics.IN_FLIGHT.inc()

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception:
            logger.exception(
                "unhandled error",
                extra={"method": scope["method"], "path": scope["path"]},
            )
            raise
        finally:
            latency_ms = round((time.perf_counter() - start) * 1000, 2)
            logger.info(
                "request completed",
                extra={
                    "method": scope["method"],
                    "path": scope["path"],
                    "status_code": status_code,
                    "latency_ms": latency_ms,
                },
            )
            metrics.IN_FLIGHT.dec()
            self._observe(scope, usage, status_code, latency_ms)
            self._record_usage(
                scope, usage, request_id, scope["method"], status_code, latency_ms, started_at
            )
            usage_ctx.reset(usage_token)
            request_id_ctx.reset(token)

    @staticmethod
    def _observe(scope: Scope, usage: UsageContext, status_code: int, latency_ms: float) -> None:
        try:
            metrics.observe_request(
                method=scope["method"],
                status_code=status_code,
                latency_seconds=latency_ms / 1000,
                route=usage.route_prefix if usage.recordable else None,
                outcome=usage.outcome,
            )
        except Exception:  # metrics must never break a request
            logger.exception("could not record metrics")

    @staticmethod
    def _record_usage(
        scope: Scope,
        usage: UsageContext,
        request_id: str,
        method: str,
        status_code: int,
        latency_ms: float,
        started_at: datetime,
    ) -> None:
        if not usage.recordable:
            return
        recorder = getattr(scope["app"].state, "usage_recorder", None)
        if recorder is None:
            return
        try:
            recorder.record(
                UsageRecord(
                    tenant_id=usage.tenant_id,
                    route_id=usage.route_id,
                    route_prefix=usage.route_prefix,
                    request_id=request_id,
                    method=method,
                    status_code=status_code,
                    outcome=usage.outcome,
                    latency_ms=latency_ms,
                    occurred_at=started_at,
                )
            )
        except Exception:  # recording must never break a request
            logger.exception("could not record usage")
