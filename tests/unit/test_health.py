import pytest
from httpx import ASGITransport, AsyncClient

from app.api import health
from app.main import app


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_live_returns_ok(client):
    response = await client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_response_carries_request_id(client):
    response = await client.get("/health/live")
    assert response.headers["X-Request-ID"].startswith("req_")


async def test_inbound_request_id_is_reused(client):
    response = await client.get("/health/live", headers={"X-Request-ID": "abc-123"})
    assert response.headers["X-Request-ID"] == "abc-123"


async def test_ready_when_dependencies_up(client, monkeypatch):
    async def ok(*_args):
        return None

    monkeypatch.setattr(health, "check_database", lambda: ok())
    monkeypatch.setattr(health, "check_redis", lambda _app: ok())
    response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["checks"] == {"postgres": "ok", "redis": "ok"}


async def test_ready_503_when_redis_down(client, monkeypatch):
    async def ok(*_args):
        return None

    async def fail(*_args):
        raise ConnectionError("redis down")

    monkeypatch.setattr(health, "check_database", lambda: ok())
    monkeypatch.setattr(health, "check_redis", lambda _app: fail())
    response = await client.get("/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"] == {"postgres": "ok", "redis": "unavailable"}
