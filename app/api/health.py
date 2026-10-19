import asyncio
import logging

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import SessionLocal

logger = logging.getLogger("rateforge.health")
router = APIRouter(prefix="/health", tags=["health"])


async def check_database() -> None:
    async with SessionLocal() as session:
        await session.execute(text("SELECT 1"))


async def check_redis(app: FastAPI) -> None:
    await app.state.redis.ping()


async def _probe(name: str, coro) -> tuple[str, bool]:
    timeout = get_settings().health_check_timeout_seconds
    try:
        await asyncio.wait_for(coro, timeout=timeout)
        return name, True
    except Exception as exc:  # any failure means "not ready"
        logger.warning("dependency check failed", extra={"path": name})
        logger.debug("detail: %r", exc)
        return name, False


@router.get("/live")
async def live() -> dict[str, str]:
    """Liveness: the process is up. Never touches dependencies."""
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    """Readiness: required dependencies are reachable."""
    results = await asyncio.gather(
        _probe("postgres", check_database()),
        _probe("redis", check_redis(request.app)),
    )
    checks = {name: ("ok" if ok else "unavailable") for name, ok in results}
    all_ok = all(ok for _, ok in results)
    return JSONResponse(
        status_code=200 if all_ok else 503,
        content={"status": "ready" if all_ok else "degraded", "checks": checks},
    )
