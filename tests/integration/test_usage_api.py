import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert

from app.models import UsageEvent


def hour_ago(hours: float = 1) -> datetime:
    return datetime.now(UTC) - timedelta(hours=hours)


async def seed(db, tenant_id: str, rows: list[dict]) -> None:
    """Insert events directly, so the maths can be checked against known values."""
    defaults = {
        "tenant_id": uuid.UUID(tenant_id),
        "route_id": None,
        "route_prefix": "echo",
        "request_id": "req_seed",
        "method": "GET",
        "status_code": 200,
        "outcome": "forwarded",
        "latency_ms": 10.0,
        "occurred_at": hour_ago(),
    }
    await db.execute(insert(UsageEvent), [{**defaults, **row} for row in rows])
    await db.commit()


# ---------------- access control ----------------
async def test_usage_endpoints_require_credentials(client):
    assert (await client.get("/api/v1/usage")).status_code == 401
    assert (await client.get("/api/v1/usage/summary")).status_code == 401


async def test_a_tenant_sees_only_its_own_events(client, db, make_tenant, make_key):
    a, b = await make_tenant("a"), await make_tenant("b")
    key_a = await make_key(a["id"])
    await seed(db, a["id"], [{"request_id": "a1"}, {"request_id": "a2"}])
    await seed(db, b["id"], [{"request_id": "b1"}])

    headers = {"X-API-Key": key_a["api_key"]}
    listed = (await client.get("/api/v1/usage", headers=headers)).json()
    assert sorted(i["request_id"] for i in listed["items"]) == ["a1", "a2"]

    # Naming another tenant must not widen the view, in the list or the summary.
    sneaky = (await client.get(f"/api/v1/usage?tenant_id={b['id']}", headers=headers)).json()
    assert sorted(i["request_id"] for i in sneaky["items"]) == ["a1", "a2"]
    summary = await client.get(f"/api/v1/usage/summary?tenant_id={b['id']}", headers=headers)
    assert summary.json()["total_requests"] == 2


async def test_admin_sees_everyone_or_one_tenant(client, admin, db, make_tenant):
    a, b = await make_tenant("a"), await make_tenant("b")
    await seed(db, a["id"], [{}, {}])
    await seed(db, b["id"], [{}])
    assert len((await client.get("/api/v1/usage", headers=admin)).json()["items"]) == 3
    only_b = (await client.get(f"/api/v1/usage?tenant_id={b['id']}", headers=admin)).json()
    assert [i["tenant_id"] for i in only_b["items"]] == [b["id"]]
    summary = (await client.get("/api/v1/usage/summary", headers=admin)).json()
    assert summary["total_requests"] == 3


async def test_responses_never_contain_secrets(client, admin, db, make_tenant, make_key):
    tenant = await make_tenant()
    key = await make_key(tenant["id"])
    await seed(db, tenant["id"], [{}])
    text = (await client.get("/api/v1/usage", headers=admin)).text
    assert key["api_key"] not in text and key["key_prefix"] not in text


# ---------------- listing, filters, ordering ----------------
async def test_events_come_newest_first(client, admin, db, make_tenant):
    tenant = await make_tenant()
    await seed(
        db,
        tenant["id"],
        [
            {"request_id": "old", "occurred_at": hour_ago(3)},
            {"request_id": "new", "occurred_at": hour_ago(1)},
            {"request_id": "mid", "occurred_at": hour_ago(2)},
        ],
    )
    items = (await client.get("/api/v1/usage", headers=admin)).json()["items"]
    assert [i["request_id"] for i in items] == ["new", "mid", "old"]


async def test_filters(client, admin, db, make_tenant):
    tenant = await make_tenant()
    await seed(
        db,
        tenant["id"],
        [
            {"request_id": "ok-echo"},
            {"request_id": "ok-billing", "route_prefix": "billing"},
            {"request_id": "limited", "outcome": "rate_limited", "status_code": 429},
            {"request_id": "boom", "status_code": 502},
        ],
    )

    async def ids(query: str) -> list[str]:
        response = await client.get(f"/api/v1/usage?{query}", headers=admin)
        assert response.status_code == 200, response.text
        return sorted(i["request_id"] for i in response.json()["items"])

    assert await ids("route=billing") == ["ok-billing"]
    assert await ids("status_code=429") == ["limited"]
    assert await ids("status_code=502") == ["boom"]
    assert await ids("outcome=rate_limited") == ["limited"]
    assert await ids("outcome=forwarded&route=echo") == ["boom", "ok-echo"]
    assert await ids("route=nothing-here") == []


async def test_time_range_is_inclusive_start_exclusive_end(client, admin, db, make_tenant):
    tenant = await make_tenant()
    start = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=3)
    await seed(
        db,
        tenant["id"],
        [
            {"request_id": "before", "occurred_at": start - timedelta(seconds=1)},
            {"request_id": "at-start", "occurred_at": start},
            {"request_id": "inside", "occurred_at": start + timedelta(hours=1)},
            {"request_id": "at-end", "occurred_at": start + timedelta(hours=2)},
        ],
    )
    end = start + timedelta(hours=2)
    response = await client.get(
        "/api/v1/usage", params={"start": start.isoformat(), "end": end.isoformat()}, headers=admin
    )
    assert sorted(i["request_id"] for i in response.json()["items"]) == ["at-start", "inside"]


async def test_default_range_is_the_last_24_hours(client, admin, db, make_tenant):
    tenant = await make_tenant()
    await seed(
        db,
        tenant["id"],
        [
            {"request_id": "recent", "occurred_at": hour_ago(2)},
            {"request_id": "two-days-old", "occurred_at": hour_ago(48)},
        ],
    )
    items = (await client.get("/api/v1/usage", headers=admin)).json()["items"]
    assert [i["request_id"] for i in items] == ["recent"]
    wide = await client.get(
        "/api/v1/usage", params={"start": hour_ago(72).isoformat()}, headers=admin
    )
    assert len(wide.json()["items"]) == 2


async def test_timestamps_without_a_timezone_are_treated_as_utc(client, admin, db, make_tenant):
    tenant = await make_tenant()
    await seed(db, tenant["id"], [{"occurred_at": hour_ago(2)}])
    naive_start = (datetime.now(UTC) - timedelta(hours=3)).replace(tzinfo=None).isoformat()
    response = await client.get("/api/v1/usage", params={"start": naive_start}, headers=admin)
    assert response.status_code == 200 and len(response.json()["items"]) == 1


# ---------------- pagination ----------------
async def _all_pages(client, admin, limit: int, extra: str = "") -> list[list[dict]]:
    pages, cursor = [], None
    while True:
        query = f"limit={limit}{extra}" + (f"&cursor={cursor}" if cursor else "")
        body = (await client.get(f"/api/v1/usage?{query}", headers=admin)).json()
        pages.append(body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            return pages


async def test_pagination_walks_every_event_exactly_once(client, admin, db, make_tenant):
    tenant = await make_tenant()
    await seed(
        db,
        tenant["id"],
        [
            {"request_id": f"r{i:02d}", "occurred_at": hour_ago(1) + timedelta(seconds=i)}
            for i in range(25)
        ],
    )
    pages = await _all_pages(client, admin, limit=10)
    assert [len(p) for p in pages] == [10, 10, 5]
    ids = [i["request_id"] for page in pages for i in page]
    assert ids == [f"r{i:02d}" for i in range(24, -1, -1)]  # newest first, no gaps, no repeats


async def test_page_that_exactly_fills_the_limit_has_no_phantom_next_page(
    client, admin, db, make_tenant
):
    tenant = await make_tenant()
    await seed(db, tenant["id"], [{} for _ in range(10)])
    body = (await client.get("/api/v1/usage?limit=10", headers=admin)).json()
    assert len(body["items"]) == 10 and body["next_cursor"] is None


async def test_events_with_identical_timestamps_are_not_skipped_or_repeated(
    client, admin, db, make_tenant
):
    tenant = await make_tenant()
    same = hour_ago(1)
    await seed(db, tenant["id"], [{"request_id": f"t{i}", "occurred_at": same} for i in range(12)])
    pages = await _all_pages(client, admin, limit=5)
    ids = [i["request_id"] for page in pages for i in page]
    assert sorted(ids) == sorted(f"t{i}" for i in range(12))
    assert len(ids) == len(set(ids))


async def test_new_events_do_not_shift_pages_already_in_progress(client, admin, db, make_tenant):
    tenant = await make_tenant()
    base = hour_ago(2)
    await seed(
        db,
        tenant["id"],
        [
            {"request_id": f"r{i:02d}", "occurred_at": base + timedelta(seconds=i)}
            for i in range(10)
        ],
    )
    first = (await client.get("/api/v1/usage?limit=4", headers=admin)).json()
    # Traffic keeps arriving between page requests (this one is newer than everything above).
    await seed(db, tenant["id"], [{"request_id": "late-arrival", "occurred_at": hour_ago(0.1)}])
    second = (
        await client.get(f"/api/v1/usage?limit=4&cursor={first['next_cursor']}", headers=admin)
    ).json()
    assert [i["request_id"] for i in first["items"]] == ["r09", "r08", "r07", "r06"]
    assert [i["request_id"] for i in second["items"]] == ["r05", "r04", "r03", "r02"]  # no shift


# ---------------- validation ----------------
@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=501",
        "cursor=not-a-cursor",
        "status_code=99",
        "status_code=600",
        "outcome=bogus",
        "tenant_id=not-a-uuid",
        "start=garbage",
    ],
)
async def test_bad_parameters_are_422(client, admin, query):
    assert (await client.get(f"/api/v1/usage?{query}", headers=admin)).status_code == 422


async def test_time_range_validation(client, admin):
    now = datetime.now(UTC)
    backwards = {"start": now.isoformat(), "end": (now - timedelta(hours=1)).isoformat()}
    assert (await client.get("/api/v1/usage", params=backwards, headers=admin)).status_code == 422
    too_wide = {"start": (now - timedelta(days=400)).isoformat(), "end": now.isoformat()}
    assert (await client.get("/api/v1/usage", params=too_wide, headers=admin)).status_code == 422


# ---------------- summary maths ----------------
LATENCIES = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]


async def _seed_known_traffic(db, tenant_id):
    rows = [{"latency_ms": float(ms)} for ms in LATENCIES]  # 10 x 200
    rows += [{"status_code": 500, "latency_ms": 200.0}, {"status_code": 502, "latency_ms": 300.0}]
    rows += [{"outcome": "rate_limited", "status_code": 429, "latency_ms": 1.0}] * 3
    rows += [{"outcome": "quota_exceeded", "status_code": 429, "latency_ms": 1.0}]
    rows += [{"outcome": "rejected", "status_code": 405, "latency_ms": 1.0}]
    rows += [{"route_prefix": "billing", "latency_ms": 5.0}] * 3
    await seed(db, tenant_id, rows)


async def test_summary_counts_and_rates(client, admin, db, make_tenant):
    tenant = await make_tenant()
    await _seed_known_traffic(db, tenant["id"])
    body = (await client.get("/api/v1/usage/summary?route=echo", headers=admin)).json()

    assert body["total_requests"] == 17
    assert body["accepted"] == 12
    assert body["rejected"] == {"total": 5, "rate_limited": 3, "quota_exceeded": 1, "other": 1}
    assert body["status_classes"] == {"2xx": 10, "3xx": 0, "4xx": 5, "5xx": 2}
    assert body["error_rate"] == pytest.approx(2 / 12, abs=1e-4)  # 5xx among forwarded only


async def test_summary_latency_percentiles_use_forwarded_requests_only(
    client, admin, db, make_tenant
):
    tenant = await make_tenant()
    await _seed_known_traffic(db, tenant["id"])
    latency = (await client.get("/api/v1/usage/summary?route=echo", headers=admin)).json()[
        "latency_ms"
    ]
    # 12 forwarded latencies: 10..100 by tens, then 200 and 300. The 1 ms rejections are excluded.
    assert latency["avg"] == pytest.approx(87.5)
    assert latency["p50"] == pytest.approx(65.0)  # between the 6th (60) and 7th (70)
    assert latency["p95"] == pytest.approx(245.0)  # 200 + 0.45 * (300 - 200)
    assert latency["p99"] == pytest.approx(289.0)  # 200 + 0.89 * (300 - 200)


async def test_summary_top_routes(client, admin, db, make_tenant):
    tenant = await make_tenant()
    await _seed_known_traffic(db, tenant["id"])
    body = (await client.get("/api/v1/usage/summary", headers=admin)).json()
    assert body["total_requests"] == 20
    echo, billing = body["top_routes"]  # ordered by request count
    assert (echo["route"], echo["requests"], echo["errors"]) == ("echo", 17, 2)
    assert echo["error_rate"] == pytest.approx(2 / 12, abs=1e-4)
    assert echo["p95_latency_ms"] == pytest.approx(245.0)
    assert (billing["route"], billing["requests"], billing["errors"]) == ("billing", 3, 0)
    assert billing["error_rate"] == 0.0
    assert billing["p95_latency_ms"] == pytest.approx(5.0)


async def test_summary_timeline_buckets(client, admin, db, make_tenant):
    tenant = await make_tenant()
    top = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=5)
    await seed(
        db,
        tenant["id"],
        [
            {"occurred_at": top + timedelta(minutes=5)},
            {
                "occurred_at": top + timedelta(minutes=50),
                "outcome": "rate_limited",
                "status_code": 429,
            },
            {"occurred_at": top + timedelta(minutes=70), "status_code": 500},
        ],
    )
    body = (await client.get("/api/v1/usage/summary?bucket=hour", headers=admin)).json()
    assert [(p["requests"], p["rejected"], p["errors"]) for p in body["timeline"]] == [
        (2, 1, 0),
        (1, 0, 1),
    ]
    stamps = [datetime.fromisoformat(p["bucket"]) for p in body["timeline"]]
    assert stamps == [top, top + timedelta(hours=1)]  # aligned to the hour, in UTC

    # One-minute buckets need a narrower window: 24 hours would be 1,440 buckets (over the limit).
    window = {
        "bucket": "minute",
        "start": (top - timedelta(hours=1)).isoformat(),
        "end": (top + timedelta(hours=3)).isoformat(),
    }
    by_minute = (await client.get("/api/v1/usage/summary", params=window, headers=admin)).json()
    assert len(by_minute["timeline"]) == 3  # only minutes that contain events are listed


async def test_summary_with_no_traffic_is_all_zeros_and_nulls(client, admin):
    body = (await client.get("/api/v1/usage/summary", headers=admin)).json()
    assert body["total_requests"] == 0 and body["accepted"] == 0
    assert body["rejected"]["total"] == 0
    assert body["error_rate"] is None  # no forwarded requests: a rate would be meaningless
    assert body["latency_ms"] == {"avg": None, "p50": None, "p95": None, "p99": None}
    assert body["top_routes"] == [] and body["timeline"] == []


async def test_summary_only_rejections_has_no_latency_or_error_rate(client, admin, db, make_tenant):
    tenant = await make_tenant()
    await seed(db, tenant["id"], [{"outcome": "rate_limited", "status_code": 429}] * 4)
    body = (await client.get("/api/v1/usage/summary", headers=admin)).json()
    assert body["total_requests"] == 4 and body["accepted"] == 0
    assert body["error_rate"] is None and body["latency_ms"]["p95"] is None


async def test_summary_respects_the_time_range(client, admin, db, make_tenant):
    tenant = await make_tenant()
    await seed(db, tenant["id"], [{"occurred_at": hour_ago(2)}, {"occurred_at": hour_ago(30)}])
    assert (await client.get("/api/v1/usage/summary", headers=admin)).json()["total_requests"] == 1
    wide = await client.get(
        "/api/v1/usage/summary", params={"start": hour_ago(48).isoformat()}, headers=admin
    )
    assert wide.json()["total_requests"] == 2


async def test_summary_validation(client, admin):
    assert (await client.get("/api/v1/usage/summary?bucket=week", headers=admin)).status_code == 422
    too_many_buckets = {
        "start": (datetime.now(UTC) - timedelta(days=30)).isoformat(),
        "bucket": "minute",
    }
    response = await client.get("/api/v1/usage/summary", params=too_many_buckets, headers=admin)
    assert response.status_code == 422 and "bucket" in response.json()["detail"]
    assert (
        await client.get(
            "/api/v1/usage/summary", params={**too_many_buckets, "bucket": "day"}, headers=admin
        )
    ).status_code == 200


# ---------------- end to end: real gateway traffic -> summary ----------------
async def test_gateway_traffic_shows_up_in_the_summary(client, gateway, upstream, recorder):
    import httpx

    ctx = await gateway(limit=5, window=3600)
    statuses = iter([200, 200, 200, 500, 404, 200, 200, 200])

    def respond(request):
        return httpx.Response(next(statuses))

    upstream.responder = respond
    for _ in range(8):  # 5 are forwarded, 3 hit the rate limit
        await client.get("/gateway/echo/x", headers=ctx["headers"])
    await recorder.flush()

    body = (await client.get("/api/v1/usage/summary", headers=ctx["headers"])).json()
    assert body["total_requests"] == 8
    assert body["accepted"] == 5
    assert body["rejected"]["rate_limited"] == 3
    assert body["status_classes"] == {"2xx": 3, "3xx": 0, "4xx": 4, "5xx": 1}
    assert body["error_rate"] == pytest.approx(1 / 5)
    assert body["latency_ms"]["p50"] > 0
    assert body["top_routes"][0]["route"] == "echo"

    listed = (await client.get("/api/v1/usage?outcome=rate_limited", headers=ctx["headers"])).json()
    assert len(listed["items"]) == 3 and {i["status_code"] for i in listed["items"]} == {429}
