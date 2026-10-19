import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis

from app.api import health
from app.api.v1 import api_router
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.middleware import register_middleware
from app.db.session import engine

logger = logging.getLogger("rateforge")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    app.state.redis = Redis.from_url(settings.redis_url, decode_responses=True)
    logger.info("rateforge starting", extra={"path": settings.environment})
    yield
    await app.state.redis.aclose()
    await engine.dispose()
    logger.info("rateforge stopped")


def create_app() -> FastAPI:
    app = FastAPI(title="RateForge", version="0.1.0", lifespan=lifespan)
    register_middleware(app)
    app.include_router(health.router)
    app.include_router(api_router)
    return app


app = create_app()
