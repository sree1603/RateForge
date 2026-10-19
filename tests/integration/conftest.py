"""Integration tests run against a real PostgreSQL (not SQLite) so constraints, row locks
and timestamp behaviour match production.

Set TEST_DATABASE_URL to override; by default the database named in DATABASE_URL gets a
`_test` suffix. The database is created on demand and its tables are rebuilt per test.
"""

import os
from collections.abc import AsyncIterator

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.db.base import Base
from app.db.session import get_session
from app.main import app
from app.models import Plan, Tenant  # noqa: F401  (ensure models are registered)


def _test_url() -> str:
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        return explicit
    url = make_url(get_settings().database_url)
    return url.set(database=f"{url.database}_test").render_as_string(hide_password=False)


async def _ensure_database_exists(url: str) -> None:
    parsed = make_url(url)
    admin = parsed.set(drivername="postgresql", database="postgres")
    kwargs = {
        "user": admin.username,
        "password": admin.password,
        "host": admin.query.get("host") or admin.host,
        "port": admin.port,
        "database": "postgres",
    }
    conn = await asyncpg.connect(**{k: v for k, v in kwargs.items() if v is not None})
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", parsed.database
        )
        if not exists:
            await conn.execute(f'CREATE DATABASE "{parsed.database}"')
    finally:
        await conn.close()


@pytest.fixture
async def engine():
    url = _test_url()
    try:
        await _ensure_database_exists(url)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"PostgreSQL not reachable for integration tests: {exc}")
    eng = create_async_engine(url, poolclass=NullPool)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session_factory(engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def db(session_factory) -> AsyncIterator[AsyncSession]:
    """A session for the test body to inspect or modify the database directly."""
    async with session_factory() as session:
        yield session


@pytest.fixture
async def client(session_factory) -> AsyncIterator[AsyncClient]:
    async def override_get_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def admin() -> dict[str, str]:
    return {"X-Admin-Token": os.environ["ADMIN_TOKEN"]}


@pytest.fixture
async def make_tenant(client, admin):
    async def _make(name: str = "acme") -> dict:
        response = await client.post("/api/v1/tenants", json={"name": name}, headers=admin)
        assert response.status_code == 201, response.text
        return response.json()

    return _make


@pytest.fixture
async def make_key(client, admin):
    """Create a key for a tenant (as admin). Returns the full creation response."""

    async def _make(tenant_id: str, name: str = "default", **extra) -> dict:
        body = {"tenant_id": tenant_id, "name": name, **extra}
        response = await client.post("/api/v1/keys", json=body, headers=admin)
        assert response.status_code == 201, response.text
        return response.json()

    return _make
