import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from app.models import ApiKey, Tenant


async def test_create_key_returns_secret_once_and_never_stores_it(
    client, db, make_tenant, make_key
):
    tenant = await make_tenant()
    created = await make_key(tenant["id"], "ci-server")
    raw = created["api_key"]
    assert raw.startswith("rf_")

    row = (await db.execute(select(ApiKey))).scalar_one()
    assert row.key_prefix == created["key_prefix"]
    # The database holds a 64-char HMAC digest, never the key or any part of its secret.
    assert len(row.key_hash) == 64
    secret_part = raw.split("_", 2)[2]
    assert raw not in row.key_hash and secret_part not in row.key_hash
    assert secret_part not in row.key_prefix


async def test_listing_never_exposes_secret_or_hash(client, make_tenant, make_key):
    tenant = await make_tenant()
    created = await make_key(tenant["id"])
    listed = await client.get("/api/v1/keys", headers={"X-API-Key": created["api_key"]})
    assert listed.status_code == 200
    body = listed.json()
    assert len(body) == 1
    assert "api_key" not in body[0] and "key_hash" not in body[0]
    assert created["api_key"] not in listed.text


async def test_admin_must_name_a_tenant(client, admin):
    response = await client.post("/api/v1/keys", json={"name": "x"}, headers=admin)
    assert response.status_code == 422


async def test_key_for_unknown_tenant_is_404(client, admin):
    response = await client.post(
        "/api/v1/keys", json={"name": "x", "tenant_id": str(uuid.uuid4())}, headers=admin
    )
    assert response.status_code == 404


async def test_tenant_can_issue_key_for_itself_only(client, make_tenant, make_key):
    mine = await make_tenant("mine")
    other = await make_tenant("other")
    first = await make_key(mine["id"])
    headers = {"X-API-Key": first["api_key"]}

    own = await client.post("/api/v1/keys", json={"name": "second"}, headers=headers)
    assert own.status_code == 201
    assert own.json()["tenant_id"] == mine["id"]

    stranger = await client.post(
        "/api/v1/keys", json={"name": "sneaky", "tenant_id": other["id"]}, headers=headers
    )
    assert stranger.status_code == 403


async def test_tenant_only_sees_its_own_keys(client, make_tenant, make_key):
    a = await make_tenant("a")
    b = await make_tenant("b")
    key_a = await make_key(a["id"])
    await make_key(b["id"])
    # Passing someone else's tenant_id as a query parameter must not widen the view.
    response = await client.get(
        f"/api/v1/keys?tenant_id={b['id']}", headers={"X-API-Key": key_a["api_key"]}
    )
    assert [k["tenant_id"] for k in response.json()] == [a["id"]]


async def test_admin_can_list_all_or_filter(client, admin, make_tenant, make_key):
    a = await make_tenant("a")
    b = await make_tenant("b")
    await make_key(a["id"])
    await make_key(b["id"])
    everything = await client.get("/api/v1/keys", headers=admin)
    assert len(everything.json()) == 2
    only_b = await client.get(f"/api/v1/keys?tenant_id={b['id']}", headers=admin)
    assert [k["tenant_id"] for k in only_b.json()] == [b["id"]]


async def test_revoked_key_stops_working(client, make_tenant, make_key):
    tenant = await make_tenant()
    key = await make_key(tenant["id"])
    headers = {"X-API-Key": key["api_key"]}
    assert (await client.get("/api/v1/keys", headers=headers)).status_code == 200

    other = await make_key(tenant["id"], "other")  # a second key to perform the revoke with
    revoke = await client.delete(
        f"/api/v1/keys/{key['id']}", headers={"X-API-Key": other["api_key"]}
    )
    assert revoke.status_code == 204
    assert (await client.get("/api/v1/keys", headers=headers)).status_code == 401


async def test_revoke_is_idempotent(client, admin, make_tenant, make_key):
    tenant = await make_tenant()
    key = await make_key(tenant["id"])
    for _ in range(2):
        response = await client.delete(f"/api/v1/keys/{key['id']}", headers=admin)
        assert response.status_code == 204


async def test_tenant_cannot_revoke_another_tenants_key(client, make_tenant, make_key):
    a = await make_tenant("a")
    b = await make_tenant("b")
    key_a = await make_key(a["id"])
    key_b = await make_key(b["id"])
    response = await client.delete(
        f"/api/v1/keys/{key_b['id']}", headers={"X-API-Key": key_a["api_key"]}
    )
    assert response.status_code == 404  # same answer as "no such key"
    # ...and B's key still works.
    assert (
        await client.get("/api/v1/keys", headers={"X-API-Key": key_b["api_key"]})
    ).status_code == 200


async def test_revoke_unknown_key_is_404(client, admin):
    response = await client.delete(f"/api/v1/keys/{uuid.uuid4()}", headers=admin)
    assert response.status_code == 404


async def test_expired_key_is_rejected(client, db, make_tenant, make_key):
    tenant = await make_tenant()
    key = await make_key(tenant["id"], expires_in_days=30)
    assert key["expires_at"] is not None
    headers = {"X-API-Key": key["api_key"]}
    assert (await client.get("/api/v1/keys", headers=headers)).status_code == 200

    await db.execute(update(ApiKey).values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    await db.commit()
    assert (await client.get("/api/v1/keys", headers=headers)).status_code == 401


async def test_suspended_tenant_gets_403_and_cannot_get_new_keys(
    client, admin, db, make_tenant, make_key
):
    tenant = await make_tenant()
    key = await make_key(tenant["id"])
    await db.execute(update(Tenant).values(status="suspended"))
    await db.commit()

    response = await client.get("/api/v1/keys", headers={"X-API-Key": key["api_key"]})
    assert response.status_code == 403

    new = await client.post(
        "/api/v1/keys", json={"name": "x", "tenant_id": tenant["id"]}, headers=admin
    )
    assert new.status_code == 409


async def test_last_used_is_set_and_throttled(client, db, make_tenant, make_key):
    tenant = await make_tenant()
    key = await make_key(tenant["id"])
    headers = {"X-API-Key": key["api_key"]}

    await client.get("/api/v1/keys", headers=headers)
    first = (await db.execute(select(ApiKey.last_used_at))).scalar_one()
    assert first is not None

    await client.get("/api/v1/keys", headers=headers)
    db.expire_all()
    second = (await db.execute(select(ApiKey.last_used_at))).scalar_one()
    assert second == first  # within the throttle window: no second write
