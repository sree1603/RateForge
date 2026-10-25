from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

_settings = get_settings()
engine = create_async_engine(
    _settings.database_url,
    pool_pre_ping=True,  # replace connections the server closed, at the cost of one round trip
    pool_size=_settings.db_pool_size,
    max_overflow=_settings.db_max_overflow,
    pool_timeout=_settings.db_pool_timeout_seconds,
    connect_args={"timeout": _settings.db_connect_timeout_seconds},
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request."""
    async with SessionLocal() as session:
        yield session
