"""Application startup and shutdown, run for real (the other tests skip the lifespan)."""

import logging

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.main import create_app


@pytest.fixture
def restore_logging():
    """Starting the app reconfigures the root logger; undo that so other tests are unaffected."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers, root.level = handlers, level
    logging.getLogger("uvicorn.access").disabled = False


async def test_startup_creates_the_shared_clients_and_shutdown_closes_them(
    redis_client, redis_url, restore_logging, monkeypatch
):
    monkeypatch.setattr(get_settings(), "redis_url", redis_url)  # the real startup reads this
    app = create_app()
    async with app.router.lifespan_context(app):
        assert app.state.http_client.is_closed is False
        assert app.state.rate_limiter is not None
        assert app.state.usage_recorder._task is not None  # background writer is running
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            assert (await client.get("/health/live")).json() == {"status": "ok"}
            ready = await client.get("/health/ready")
            assert ready.status_code == 200, ready.text
            assert ready.json()["checks"] == {"postgres": "ok", "redis": "ok"}
        http_client = app.state.http_client
    assert http_client.is_closed is True
    assert app.state.usage_recorder._task is None  # stopped and flushed


async def test_usage_recording_can_be_switched_off(
    redis_client, redis_url, restore_logging, monkeypatch
):
    monkeypatch.setattr(get_settings(), "redis_url", redis_url)
    monkeypatch.setattr(get_settings(), "usage_recording_enabled", False)
    app = create_app()
    async with app.router.lifespan_context(app):
        assert not hasattr(app.state, "usage_recorder")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            assert (await client.get("/health/live")).status_code == 200


async def test_startup_succeeds_even_if_redis_is_down(restore_logging, monkeypatch):
    """The gateway must start and report 'not ready' rather than crash-loop; Redis clients
    connect lazily, so an outage shows up in /health/ready and in request handling instead."""
    monkeypatch.setattr(get_settings(), "redis_url", "redis://127.0.0.1:1/0")
    app = create_app()
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            ready = await client.get("/health/ready")
            assert ready.status_code == 503
            assert ready.json()["checks"]["redis"] == "unavailable"
            assert (await client.get("/health/live")).status_code == 200
