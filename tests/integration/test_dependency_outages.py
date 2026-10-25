"""What clients see when PostgreSQL cannot be reached: a clear, retryable 503 and no internals,
never a bare 500."""

import time

import pytest
from httpx import ASGITransport, AsyncClient
from prometheus_client import REGISTRY
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.session import get_session
from app.main import app

LEAKS = ("Traceback", "asyncpg", "sqlalchemy", "127.0.0.1", "Errno", "ConnectionRefused")


def point_database_at(engine) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = override


def unreachable_engine():
    # Nothing listens on port 1, so connecting is refused immediately.
    return create_async_engine(
        "postgresql+asyncpg://postgres@127.0.0.1:1/none",
        poolclass=NullPool,
        connect_args={"timeout": 0.5},
    )


def assert_clean_503(response) -> None:
    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert response.json() == {"detail": "Service temporarily unavailable; try again shortly"}
    assert not any(leak in response.text for leak in LEAKS)
    assert response.headers["x-request-id"].startswith("req_")


async def test_admin_api_returns_503_when_the_database_is_down(client, admin):
    point_database_at(unreachable_engine())
    assert_clean_503(await client.get("/api/v1/tenants", headers=admin))


async def test_gateway_returns_503_when_the_database_is_down(client, gateway):
    ctx = await gateway()
    point_database_at(unreachable_engine())
    assert_clean_503(await client.get("/gateway/echo/x", headers=ctx["headers"]))


async def test_a_database_outage_is_fast_not_a_hang(client, admin):
    point_database_at(unreachable_engine())
    started = time.perf_counter()
    await client.get("/api/v1/tenants", headers=admin)
    assert time.perf_counter() - started < 3


async def test_unroutable_database_times_out_instead_of_hanging(client, admin):
    # 192.0.2.1 (TEST-NET-1) is never routed: the connection attempt cannot succeed.
    engine = create_async_engine(
        "postgresql+asyncpg://postgres@192.0.2.1:5432/none",
        poolclass=NullPool,
        connect_args={"timeout": 0.5},
    )
    point_database_at(engine)
    started = time.perf_counter()
    response = await client.get("/api/v1/tenants", headers=admin)
    assert_clean_503(response)
    assert time.perf_counter() - started < 5


async def test_pool_exhaustion_is_a_503_not_a_500(client, admin, engine):
    """If every connection is busy for longer than the pool timeout, requests give up cleanly."""
    pool_engine = create_async_engine(
        str(engine.url.render_as_string(hide_password=False)),
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.3,
    )
    try:
        point_database_at(pool_engine)
        async with pool_engine.connect():  # hold the only connection
            assert_clean_503(await client.get("/api/v1/tenants", headers=admin))
    finally:
        await pool_engine.dispose()


async def test_the_gateway_recovers_when_the_database_comes_back(
    client, gateway, admin, session_factory
):
    ctx = await gateway()
    point_database_at(unreachable_engine())
    assert (await client.get("/gateway/echo/x", headers=ctx["headers"])).status_code == 503

    async def healthy():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = healthy
    assert (await client.get("/gateway/echo/x", headers=ctx["headers"])).status_code == 200


async def test_outages_are_counted(client, admin):
    def count():
        return (
            REGISTRY.get_sample_value(
                "rateforge_dependency_errors_total", {"dependency": "database"}
            )
            or 0.0
        )

    before = count()
    point_database_at(unreachable_engine())
    await client.get("/api/v1/tenants", headers=admin)
    assert count() - before == 1


async def test_real_bugs_still_surface_as_500(session_factory):
    """The 503 handler must be narrow: a programming error is not 'the database being down'."""

    async def broken():
        raise RuntimeError("a genuine bug")
        yield  # pragma: no cover

    app.dependency_overrides[get_session] = broken
    try:
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            response = await c.get("/api/v1/tenants", headers={"X-Admin-Token": "x" * 20})
        assert response.status_code == 500
        assert "genuine bug" not in response.text
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("path", ["/health/live"])
async def test_liveness_does_not_depend_on_the_database(client, path):
    point_database_at(unreachable_engine())
    assert (await client.get(path)).status_code == 200


async def test_readiness_reports_the_outage(client, monkeypatch):
    from app.api import health

    async def down():
        raise ConnectionRefusedError("down")

    monkeypatch.setattr(health, "check_database", down)
    response = await client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["postgres"] == "unavailable"
