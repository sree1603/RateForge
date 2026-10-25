import pytest
from prometheus_client import REGISTRY

from app.core.config import get_settings
from app.main import app
from tests.integration.test_gateway_limits import _broken_limiter


def sample(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


async def hit(client, ctx, path="/gateway/echo/x", **kwargs):
    return await client.get(path, headers=ctx["headers"], **kwargs)


# ---------------- access ----------------
async def test_metrics_require_the_admin_token(client, admin):
    assert (await client.get("/metrics")).status_code == 401
    assert (await client.get("/metrics", headers={"X-Admin-Token": "wrong"})).status_code == 401
    ok = await client.get("/metrics", headers=admin)
    assert ok.status_code == 200
    assert ok.headers["content-type"].startswith("text/plain")
    assert "rateforge_http_requests_total" in ok.text


async def test_a_prometheus_style_bearer_token_works(client):
    token = get_settings().admin_token
    assert (
        await client.get("/metrics", headers={"Authorization": f"Bearer {token}"})
    ).status_code == 200
    assert (
        await client.get("/metrics", headers={"Authorization": "Bearer nope"})
    ).status_code == 401
    assert (
        await client.get("/metrics", headers={"Authorization": f"Basic {token}"})
    ).status_code == 401


async def test_a_tenant_key_cannot_read_metrics(client, gateway):
    ctx = await gateway()
    assert (await client.get("/metrics", headers=ctx["headers"])).status_code == 401


async def test_metrics_can_be_made_public_deliberately(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "metrics_public", True)
    assert (await client.get("/metrics")).status_code == 200


# ---------------- what gets counted ----------------
async def test_gateway_requests_are_counted_by_route_outcome_and_status(client, gateway):
    ctx = await gateway(limit=2, window=3600)
    ok = (
        "rateforge_gateway_requests_total",
        {"route": "echo", "outcome": "forwarded", "status_class": "2xx"},
    )
    limited = (
        "rateforge_gateway_requests_total",
        {"route": "echo", "outcome": "rate_limited", "status_class": "4xx"},
    )
    before_ok, before_limited = sample(ok[0], **ok[1]), sample(limited[0], **limited[1])

    for _ in range(3):
        await hit(client, ctx)

    assert sample(ok[0], **ok[1]) - before_ok == 2
    assert sample(limited[0], **limited[1]) - before_limited == 1


async def test_latency_histogram_records_forwarded_requests(client, gateway):
    ctx = await gateway()
    labels = {"route": "echo", "outcome": "forwarded"}
    before = sample("rateforge_gateway_request_duration_seconds_count", **labels)
    await hit(client, ctx)
    await hit(client, ctx)
    assert sample("rateforge_gateway_request_duration_seconds_count", **labels) - before == 2
    assert sample("rateforge_gateway_request_duration_seconds_sum", **labels) > 0


async def test_all_http_requests_are_counted_by_method_and_status_class(client):
    before = sample("rateforge_http_requests_total", method="GET", status_class="2xx")
    await client.get("/health/live")
    assert sample("rateforge_http_requests_total", method="GET", status_class="2xx") - before == 1


async def test_unusual_http_methods_share_one_label(client):
    before = sample("rateforge_http_requests_total", method="OTHER", status_class="4xx")
    await client.request("BREW", "/health/live")
    assert sample("rateforge_http_requests_total", method="OTHER", status_class="4xx") - before == 1


async def test_clients_cannot_create_new_label_values(client, gateway, admin):
    """Unknown routes and made-up paths must not become metric labels: every label value is a
    time series that is never freed, so this would let anyone exhaust the metrics store."""
    ctx = await gateway()
    await hit(client, ctx, "/gateway/attacker-chosen-prefix-123/x")
    await client.get("/gateway/echo/users/987654321/secret-path", headers=ctx["headers"])
    await client.get("/gateway/another-fake-route/x")  # unauthenticated
    text = (await client.get("/metrics", headers=admin)).text
    assert "attacker-chosen-prefix-123" not in text
    assert "another-fake-route" not in text
    assert "987654321" not in text and "secret-path" not in text


async def test_metrics_never_contain_credentials_or_tenant_ids(client, gateway, admin):
    ctx = await gateway()
    await hit(client, ctx)
    text = (await client.get("/metrics", headers=admin)).text
    assert ctx["headers"]["X-API-Key"] not in text
    assert ctx["tenant"]["id"] not in text


async def test_in_flight_gauge_returns_to_its_starting_value(client, gateway):
    import asyncio

    ctx = await gateway()
    before = sample("rateforge_http_requests_in_flight")
    await asyncio.gather(*(hit(client, ctx) for _ in range(10)))
    assert sample("rateforge_http_requests_in_flight") == before


async def test_upstream_failures_are_counted_by_kind(client, gateway, upstream):
    import httpx

    ctx = await gateway()
    for kind, error in (
        ("timeout", httpx.ReadTimeout("slow")),
        ("unreachable", httpx.ConnectError("down")),
        ("pool_exhausted", httpx.PoolTimeout("busy")),
    ):
        before = sample("rateforge_upstream_errors_total", kind=kind)
        upstream.responder = lambda r, e=error: e
        await hit(client, ctx)
        assert sample("rateforge_upstream_errors_total", kind=kind) - before == 1, kind


async def test_limiter_failures_are_counted(client, gateway):
    ctx = await gateway()
    before = sample("rateforge_limiter_failures_total")
    app.state.rate_limiter = _broken_limiter()
    await hit(client, ctx)
    assert sample("rateforge_limiter_failures_total") - before == 1


async def test_usage_event_results_are_counted(client, gateway, recorder):
    ctx = await gateway()
    before = sample("rateforge_usage_events_total", result="written")
    await hit(client, ctx)
    await hit(client, ctx)
    await recorder.flush()
    assert sample("rateforge_usage_events_total", result="written") - before == 2


@pytest.mark.parametrize("path", ["/metrics"])
async def test_metrics_endpoint_is_not_in_the_public_api_docs(client, path):
    schema = (await client.get("/openapi.json")).json()
    assert path not in schema["paths"]
