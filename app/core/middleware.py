import logging
import time
import uuid

from fastapi import FastAPI, Request

from app.core.logging import request_id_ctx

logger = logging.getLogger("rateforge.request")

REQUEST_ID_HEADER = "X-Request-ID"


def register_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def request_context(request: Request, call_next):
        # Reuse an inbound ID only if it looks sane; otherwise generate our own.
        inbound = request.headers.get(REQUEST_ID_HEADER, "")
        request_id = inbound if 0 < len(inbound) <= 64 else f"req_{uuid.uuid4().hex[:16]}"
        token = request_id_ctx.set(request_id)
        start = time.perf_counter()
        try:
            response = await call_next(request)
            response.headers[REQUEST_ID_HEADER] = request_id
            logger.info(
                "request completed",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "latency_ms": round((time.perf_counter() - start) * 1000, 2),
                },
            )
            return response
        except Exception:
            logger.exception(
                "unhandled error",
                extra={"method": request.method, "path": request.url.path},
            )
            raise
        finally:
            request_id_ctx.reset(token)
