import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from redis.asyncio import Redis

from app.api import gateway, health
from app.api.v1 import api_router
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware
from app.db.session import engine

logger = logging.getLogger("rateforge")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    app.state.redis = Redis.from_url(settings.redis_url, decode_responses=True)
    app.state.http_client = httpx.AsyncClient(
        follow_redirects=False,  # an upstream must not be able to redirect us elsewhere
        trust_env=False,  # ignore HTTP(S)_PROXY variables: the gateway talks to upstreams directly
        limits=httpx.Limits(
            max_connections=settings.http_pool_max_connections,
            max_keepalive_connections=settings.http_pool_max_keepalive,
        ),
        timeout=httpx.Timeout(30.0, pool=settings.proxy_pool_timeout_seconds),
    )
    logger.info("rateforge starting", extra={"path": settings.environment})
    yield
    await app.state.http_client.aclose()
    await app.state.redis.aclose()
    await engine.dispose()
    logger.info("rateforge stopped")


def create_app() -> FastAPI:
    app = FastAPI(title="RateForge", version="0.1.0", lifespan=lifespan)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(health.router)
    app.include_router(api_router)
    app.include_router(gateway.router)
    return app


app = create_app()
