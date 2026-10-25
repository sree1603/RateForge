"""Hostile input must never produce a 500, leak data, or alter what a query means."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, insert, select

from app.models import Tenant, UsageEvent

SQL_PAYLOADS = [
    "' OR '1'='1",
    "'; DROP TABLE tenants; --",
    '" OR 1=1 --',
    "1; SELECT pg_sleep(5)",
    "' UNION SELECT key_hash FROM api_keys --",
    "%27%20OR%201%3D1",
    "\\",
    "x' AND (SELECT 1 FROM pg_sleep(5)) --",
    "Robert'); DROP TABLE students;--",
]
CONTROL_CHARACTER_PAYLOADS = [
    "with\x00nul",
    "with\x01control",
    "line\nbreak",
    "tab\there",
    "\x7fdel",
]


async def tables_intact(db) -> None:
    assert (await db.execute(select(func.count()).select_from(Tenant))).scalar_one() >= 1
    assert (await db.execute(select(func.count()).select_from(UsageEvent))).scalar_one() >= 0


@pytest.mark.parametrize("payload", SQL_PAYLOADS)
async def test_sql_text_is_stored_as_plain_data(client, admin, db, make_tenant, payload):
    await make_tenant("keeper")
    created = await client.post("/api/v1/tenants", json={"name": payload[:100]}, headers=admin)
    assert created.status_code == 201
    listed = (await client.get("/api/v1/tenants", headers=admin)).json()
    assert payload[:100] in [t["name"] for t in listed]  # stored literally, not interpreted
    await tables_intact(db)


@pytest.mark.parametrize("payload", CONTROL_CHARACTER_PAYLOADS)
@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/v1/tenants", {"name": "{p}"}),
        ("/api/v1/plans", {"name": "{p}", "requests_per_window": 1, "window_seconds": 1}),
        ("/api/v1/upstreams", {"name": "{p}", "base_url": "http://echo:9000"}),
    ],
)
async def test_control_characters_in_names_are_rejected_not_a_500(
    client, admin, payload, path, body
):
    body = {k: (v.replace("{p}", payload) if isinstance(v, str) else v) for k, v in body.items()}
    response = await client.post(path, json=body, headers=admin)
    assert response.status_code == 422, response.text


async def test_control_characters_in_a_key_name_are_rejected(client, admin, make_tenant):
    tenant = await make_tenant()
    for payload in CONTROL_CHARACTER_PAYLOADS:
        response = await client.post(
            "/api/v1/keys", json={"tenant_id": tenant["id"], "name": payload}, headers=admin
        )
        assert response.status_code == 422


@pytest.mark.parametrize("payload", SQL_PAYLOADS)
async def test_sql_in_a_usage_filter_matches_nothing_instead_of_everything(
    client, admin, db, make_tenant, payload
):
    tenant = await make_tenant()
    await db.execute(
        insert(UsageEvent),
        [
            {
                "tenant_id": uuid.UUID(tenant["id"]),
                "route_id": None,
                "route_prefix": "echo",
                "request_id": "r",
                "method": "GET",
                "status_code": 200,
                "outcome": "forwarded",
                "latency_ms": 5.0,
                "occurred_at": datetime.now(UTC) - timedelta(hours=1),
            }
        ],
    )
    await db.commit()
    everything = (await client.get("/api/v1/usage", headers=admin)).json()["items"]
    assert len(everything) == 1

    filtered = await client.get("/api/v1/usage", params={"route": payload[:64]}, headers=admin)
    assert filtered.status_code == 200
    assert filtered.json()["items"] == []  # a successful injection would have returned the row
    summary = await client.get(
        "/api/v1/usage/summary", params={"route": payload[:64]}, headers=admin
    )
    assert summary.status_code == 200 and summary.json()["total_requests"] == 0
    await tables_intact(db)


async def test_control_characters_in_a_usage_filter_are_rejected(client, admin):
    for payload in CONTROL_CHARACTER_PAYLOADS:
        assert (
            await client.get("/api/v1/usage", params={"route": payload}, headers=admin)
        ).status_code == 422


@pytest.mark.parametrize("payload", SQL_PAYLOADS + ["a\x00b", "%00", "%0d%0a", "a" * 5000])
async def test_hostile_route_prefixes_in_the_gateway_url_are_a_clean_404(client, gateway, payload):
    ctx = await gateway()
    from urllib.parse import quote

    response = await client.get(f"/gateway/{quote(payload, safe='')}/x", headers=ctx["headers"])
    assert response.status_code in (400, 404), (payload[:30], response.status_code)


@pytest.mark.parametrize("payload", SQL_PAYLOADS)
async def test_sql_in_credentials_is_just_an_invalid_credential(client, payload):
    for headers in ({"X-API-Key": payload}, {"X-Admin-Token": payload}):
        response = await client.get("/api/v1/keys", headers=headers)
        assert response.status_code == 401


@pytest.mark.parametrize(
    "bad_body",
    [
        {"name": ["a", "b"]},
        {"name": {"$ne": None}},
        {"name": 12345},
        {"name": None},
        {"name": True},
        [],
        "just a string",
    ],
)
async def test_wrong_json_types_are_422(client, admin, bad_body):
    assert (await client.post("/api/v1/tenants", json=bad_body, headers=admin)).status_code == 422


async def test_non_json_and_malformed_json_are_422(client, admin):
    for content in (b"not json", b"{", b'{"name": ', b""):
        response = await client.post(
            "/api/v1/tenants",
            content=content,
            headers={**admin, "Content-Type": "application/json"},
        )
        assert response.status_code == 422


async def test_oversized_names_are_rejected(client, admin):
    assert (
        await client.post("/api/v1/tenants", json={"name": "x" * 101}, headers=admin)
    ).status_code == 422
    big = {"name": "x" * 100_000}
    assert (await client.post("/api/v1/tenants", json=big, headers=admin)).status_code == 422


async def test_numbers_that_would_overflow_are_422(client, admin):
    for value in (2**63, 10**30, -1, 0, 1.5e300):
        response = await client.post(
            "/api/v1/plans",
            json={"name": f"p{value}"[:50], "requests_per_window": value, "window_seconds": 60},
            headers=admin,
        )
        assert response.status_code == 422, value
    response = await client.get("/api/v1/usage", params={"status_code": 2**40}, headers=admin)
    assert response.status_code == 422
