import logging
import time
import uuid

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.logging import request_id_ctx

logger = logging.getLogger("rateforge.request")

REQUEST_ID_HEADER = "X-Request-ID"


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

        # Reuse an inbound ID only if it looks sane; otherwise generate our own.
        inbound = Headers(scope=scope).get(REQUEST_ID_HEADER, "")
        request_id = inbound if 0 < len(inbound) <= 64 else f"req_{uuid.uuid4().hex[:16]}"
        token = request_id_ctx.set(request_id)
        start = time.perf_counter()
        status_code = 500

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
            logger.info(
                "request completed",
                extra={
                    "method": scope["method"],
                    "path": scope["path"],
                    "status_code": status_code,
                    "latency_ms": round((time.perf_counter() - start) * 1000, 2),
                },
            )
            request_id_ctx.reset(token)
