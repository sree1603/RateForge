"""What gets recorded when requests go through the gateway, and what must not be."""

import asyncio

import httpx
from sqlalchemy import select

from app.main import app
from app.models import UsageEvent
from app.services.usage import BufferedUsageRecorder
from tests.integration.test_gateway_limits import _broken_limiter


async def events(db):
    db.expire_all()
    return list((await db.execute(select(UsageEvent).order_by(UsageEvent.id))).scalars())


async def hit(client, ctx, path="/gateway/echo/x", method="GET", **kwargs):
    return await client.request(method, path, headers=ctx["headers"], **kwargs)


async def test_a_forwarded_request_is_recorded_with_the_facts_we_need(
    client, gateway, recorder, db
):
    ctx = await gateway()
    response = await hit(client, ctx, "/gateway/echo/orders", method="POST", content=b"{}")
    await recorder.flush()

    (event,) = await events(db)
    assert str(event.tenant_id) == ctx["tenant"]["id"]
    assert str(event.route_id) == ctx["route"]["id"]
    assert event.route_prefix == "echo"
    assert event.method == "POST"
    assert event.status_code == 200
    assert event.outcome == "forwarded"
    assert event.latency_ms > 0
    assert event.request_id == response.headers["x-request-id"]  # joins to logs and upstream
    assert event.occurred_at.tzinfo is not None


async def test_every_outcome_is_recorded_with_the_real_status(client, gateway, recorder, db):
    ctx = await gateway(limit=2, window=3600, methods=("GET",))
    await hit(client, ctx)  # forwarded 200
    await hit(client, ctx)  # forwarded 200
    await hit(client, ctx)  # rate limited
    await hit(client, ctx, method="DELETE")  # 405
    await hit(client, ctx, "/gateway/echo/%2e%2e/x")  # 400
    await recorder.flush()

    got = [(e.outcome, e.status_code) for e in await events(db)]
    assert got == [
        ("forwarded", 200),
        ("forwarded", 200),
        ("rate_limited", 429),
        ("rejected", 405),
        ("rejected", 400),
    ]


async def test_quota_rejections_are_recorded(client, gateway, recorder, db):
    ctx = await gateway(limit=1000, window=60, quota=1)
    await hit(client, ctx)
    await hit(client, ctx)
    await recorder.flush()
    assert [e.outcome for e in await events(db)] == ["forwarded", "quota_exceeded"]


async def test_upstream_failures_are_recorded_as_forwarded_with_the_gateway_status(
    client, gateway, upstream, recorder, db
):
    ctx = await gateway()
    upstream.responder = lambda r: httpx.ConnectError("down")
    await hit(client, ctx)
    upstream.responder = lambda r: httpx.Response(503)
    await hit(client, ctx)
    await recorder.flush()
    assert [(e.outcome, e.status_code) for e in await events(db)] == [
        ("forwarded", 502),
        ("forwarded", 503),
    ]


async def test_tenants_without_a_plan_and_limiter_outages_are_recorded_as_rejected(
    client, gateway, recorder, db
):
    ctx = await gateway(limit=None)
    await hit(client, ctx)  # 403: no plan
    other = await ctx["add_tenant"]("globex", limit=10, window=60)
    app.state.rate_limiter = _broken_limiter()
    await hit(client, other)  # 503: limiter unavailable (fails closed)
    await recorder.flush()
    assert [(e.outcome, e.status_code) for e in await events(db)] == [
        ("rejected", 403),
        ("rejected", 503),
    ]


async def test_requests_with_no_tenant_or_no_real_route_are_not_recorded(
    client, gateway, admin, recorder, db
):
    ctx = await gateway()
    await client.get("/gateway/echo/x")  # no key
    await client.get("/gateway/echo/x", headers={"X-API-Key": "rf_bad"})  # bad key
    await hit(client, ctx, "/gateway/no-such-route/x")  # unknown prefix
    await client.patch(
        f"/api/v1/routes/{ctx['route']['id']}", json={"enabled": False}, headers=admin
    )
    await hit(client, ctx)  # disabled route looks like an unknown one
    await client.get("/api/v1/tenants", headers=admin)  # not a gateway request at all
    await recorder.flush()
    assert await events(db) == []


async def test_no_sensitive_data_is_stored(client, gateway, recorder, db):
    ctx = await gateway()
    api_key = ctx["headers"]["X-API-Key"]
    # Key format is rf_<prefix>_<secret>, and the secret may itself contain "_": split twice only.
    secret_part = api_key.split("_", 2)[2]
    assert len(secret_part) >= 40
    await client.get(
        "/gateway/echo/users/12345/profile?token=SUPERSECRET&email=a@example.com",
        headers={**ctx["headers"], "Authorization": "Bearer TOPSECRET", "Cookie": "sid=COOKIEVAL"},
    )
    await client.post(
        "/gateway/echo/login",
        headers=ctx["headers"],
        content=b'{"password": "BODYSECRET"}',
    )
    await recorder.flush()

    rows = await events(db)
    assert len(rows) == 2
    everything = " ".join(
        str(getattr(row, column.name)) for row in rows for column in UsageEvent.__table__.columns
    )
    for secret in (api_key, secret_part, "SUPERSECRET", "a@example.com", "TOPSECRET"):
        assert secret not in everything
    for secret in ("COOKIEVAL", "BODYSECRET", "12345", "profile", "login"):
        assert secret not in everything  # not even the path: only the route prefix is kept


async def test_latency_covers_the_whole_response_not_just_the_first_byte(
    client, gateway, upstream, recorder, db
):
    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"first"
            await asyncio.sleep(0.3)
            yield b"second"

    ctx = await gateway()
    upstream.responder = lambda r: httpx.Response(200, stream=SlowBody())
    await hit(client, ctx)
    await recorder.flush()
    (event,) = await events(db)
    assert event.latency_ms >= 250


# ---------------- recording must never hurt the request ----------------
async def test_a_failing_database_does_not_affect_responses(client, gateway, db):
    class Broken:
        def __call__(self):
            raise RuntimeError("usage database is down")

    app.state.usage_recorder = BufferedUsageRecorder(Broken())
    ctx = await gateway()
    response = await hit(client, ctx)
    assert response.status_code == 200
    await app.state.usage_recorder.flush()  # must not raise
    assert app.state.usage_recorder.failed == 1


async def test_a_recorder_that_raises_does_not_affect_responses(client, gateway):
    class Exploding:
        def record(self, event):
            raise RuntimeError("boom")

    app.state.usage_recorder = Exploding()
    ctx = await gateway()
    assert (await hit(client, ctx)).status_code == 200


async def test_a_missing_recorder_is_tolerated(client, gateway):
    ctx = await gateway()
    del app.state.usage_recorder
    assert (await hit(client, ctx)).status_code == 200


async def test_concurrent_traffic_is_all_recorded(client, gateway, recorder, db):
    ctx = await gateway(limit=20, window=3600)
    await asyncio.gather(*(hit(client, ctx, f"/gateway/echo/c{i}") for i in range(50)))
    await recorder.flush()
    rows = await events(db)
    assert len(rows) == 50
    assert sum(r.outcome == "forwarded" for r in rows) == 20
    assert sum(r.outcome == "rate_limited" for r in rows) == 30
    assert len({r.request_id for r in rows}) == 50
