"""Integration tests run against a real PostgreSQL (not SQLite) so constraints, row locks
and timestamp behaviour match production.

Set TEST_DATABASE_URL to override; by default the database named in DATABASE_URL gets a
`_test` suffix. The database is created on demand and its tables are rebuilt per test.
"""

import os
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

import asyncpg
import httpx
import pytest
import redis.asyncio as aioredis
from httpx import ASGITransport, AsyncClient
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.redis import create_redis_client
from app.db.base import Base
from app.db.session import get_session
from app.main import app
from app.models import Plan, Tenant  # noqa: F401  (ensure models are registered)
from app.services.policy import policy_cache
from app.services.rate_limiter import RateLimiter
from app.services.usage import BufferedUsageRecorder


def _test_redis_url() -> str:
    """Redis database 15 (never the app's database 0), wiped around every test."""
    explicit = os.environ.get("TEST_REDIS_URL")
    if explicit:
        return explicit
    base = get_settings().redis_url.rsplit("/", 1)[0]
    return f"{base}/15"


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
def redis_url() -> str:
    return _test_redis_url()


@pytest.fixture
async def redis_client(redis_url) -> AsyncIterator[aioredis.Redis]:
    client = create_redis_client(redis_url, max_connections=100, timeout_seconds=2)
    try:
        await client.ping()
    except (aioredis.RedisError, OSError) as exc:
        await client.aclose()
        pytest.skip(f"Redis not reachable for integration tests: {exc}")
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


@pytest.fixture
async def session_factory(engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def db(session_factory) -> AsyncIterator[AsyncSession]:
    """A session for the test body to inspect or modify the database directly."""
    async with session_factory() as session:
        yield session


@dataclass
class Received:
    request: httpx.Request
    body: bytes


class FakeUpstream:
    """Stands in for the real upstream service. Records what the gateway sent and answers
    with `responder(request)`, which may return an httpx.Response or raise/return an error."""

    def __init__(self) -> None:
        self.calls: list[Received] = []
        self.responder: Callable[[httpx.Request], httpx.Response | Exception] = lambda request: (
            httpx.Response(200, json={"ok": True})
        )

    async def handle(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        self.calls.append(Received(request, body))
        result = self.responder(request)
        if isinstance(result, Exception):
            raise result
        if result.is_stream_consumed:
            # httpx pre-reads responses built with content=...; a real network response is
            # still an unread stream when it reaches the gateway, so recreate that state.
            result = httpx.Response(
                result.status_code,
                headers=result.headers,
                stream=httpx.ByteStream(result.content),
            )
        return result


@pytest.fixture
def upstream() -> FakeUpstream:
    return FakeUpstream()


@pytest.fixture
async def client(session_factory, upstream) -> AsyncIterator[AsyncClient]:
    async def override_get_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    policy_cache.clear()
    # Not started: tests call `await recorder.flush()` themselves, so results are deterministic.
    app.state.usage_recorder = BufferedUsageRecorder(session_factory)
    # The test transport does not run the app lifespan, so install the gateway's HTTP
    # client ourselves, wired to the fake upstream instead of the network.
    app.state.http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream.handle), follow_redirects=False
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await app.state.http_client.aclose()
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


@pytest.fixture
async def gateway(client, admin, make_tenant, make_key, redis_client):
    """A tenant with an API key, a plan, and a route at /gateway/echo -> http://upstream.test:9000.

    Uses the real Redis limiter. The default plan is generous so tests that are not about
    limits never hit it; pass limit/window/quota to test limiting.
    """
    app.state.rate_limiter = RateLimiter(redis_client)
    plan_counter = {"n": 0}

    async def _assign_plan(tenant_id: str, limit: int, window: int, quota: int | None) -> dict:
        plan_counter["n"] += 1
        plan = await client.post(
            "/api/v1/plans",
            json={
                "name": f"plan-{plan_counter['n']}",
                "requests_per_window": limit,
                "window_seconds": window,
                "monthly_quota": quota,
            },
            headers=admin,
        )
        assert plan.status_code == 201, plan.text
        assigned = await client.put(
            f"/api/v1/tenants/{tenant_id}/plan", json={"plan_id": plan.json()["id"]}, headers=admin
        )
        assert assigned.status_code == 200, assigned.text
        return plan.json()

    async def _add_tenant(
        name: str, limit: int = 10_000, window: int = 60, quota: int | None = None
    ):
        tenant = await make_tenant(name)
        key = await make_key(tenant["id"])
        plan = await _assign_plan(tenant["id"], limit, window, quota) if limit else None
        return {"tenant": tenant, "headers": {"X-API-Key": key["api_key"]}, "plan": plan}

    async def _setup(
        methods=("GET", "POST", "PUT", "PATCH", "DELETE"),
        base_url="http://upstream.test:9000",
        strip_prefix=True,
        limit: int | None = 10_000,
        window: int = 60,
        quota: int | None = None,
        **upstream_fields,
    ) -> dict:
        tenant = await _add_tenant("acme", limit or 0, window, quota)
        up = await client.post(
            "/api/v1/upstreams",
            json={"name": "echo", "base_url": base_url, **upstream_fields},
            headers=admin,
        )
        assert up.status_code == 201, up.text
        route = await client.post(
            "/api/v1/routes",
            json={
                "upstream_id": up.json()["id"],
                "path_prefix": "echo",
                "allowed_methods": list(methods),
                "strip_prefix": strip_prefix,
            },
            headers=admin,
        )
        assert route.status_code == 201, route.text
        return {
            **tenant,
            "upstream": up.json(),
            "route": route.json(),
            "add_tenant": _add_tenant,
            "assign_plan": _assign_plan,
        }

    return _setup


@pytest.fixture
def recorder(client) -> BufferedUsageRecorder:
    """The usage recorder wired into the app for this test."""
    return app.state.usage_recorder
