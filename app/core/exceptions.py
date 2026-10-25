"""Turn "a dependency is unreachable" into a clear, retryable 503.

Without this, a database outage surfaces as a bare 500 and a stack trace in the logs, which
looks like a bug in the gateway instead of a transient infrastructure problem. Clients get a
predictable status and a Retry-After; operators get one log line and a metric.

Deliberately narrow: only connection-level failures are mapped. A programming error (bad SQL,
a bug) must still be a 500.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import exc as sa_exc

from app.observability.metrics import DEPENDENCY_ERRORS

logger = logging.getLogger("rateforge.errors")

# OSError covers ConnectionRefusedError, ConnectionResetError, TimeoutError (asyncpg raises
# these directly when it cannot connect). The SQLAlchemy ones cover connections lost mid-query
# and the pool having no free connection within `db_pool_timeout_seconds`.
DATABASE_UNAVAILABLE = (
    OSError,
    sa_exc.OperationalError,
    sa_exc.InterfaceError,
    sa_exc.TimeoutError,
)


async def database_unavailable(request: Request, exc: Exception) -> JSONResponse:
    DEPENDENCY_ERRORS.labels(dependency="database").inc()
    logger.error("database unavailable", extra={"path": type(exc).__name__})
    return JSONResponse(
        status_code=503,
        content={"detail": "Service temporarily unavailable; try again shortly"},
        headers={"Retry-After": "1"},
    )


def register_exception_handlers(app: FastAPI) -> None:
    for exc_class in DATABASE_UNAVAILABLE:
        app.add_exception_handler(exc_class, database_unavailable)
