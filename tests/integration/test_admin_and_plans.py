from datetime import UTC, datetime

from sqlalchemy import select

from app.models import TenantPlan
from app.repositories import tenants as tenant_repo


async def test_admin_endpoints_require_credentials(client):
    assert (await client.get("/api/v1/tenants")).status_code == 401
    bad = await client.get("/api/v1/tenants", headers={"X-Admin-Token": "nope"})
    assert bad.status_code == 401


async def test_tenant_key_cannot_use_admin_endpoints(client, make_tenant, make_key):
    tenant = await make_tenant()
    key = await make_key(tenant["id"])
    response = await client.get("/api/v1/tenants", headers={"X-API-Key": key["api_key"]})
    assert response.status_code == 403


async def test_create_and_list_tenants(client, admin, make_tenant):
    await make_tenant("acme")
    await make_tenant("globex")
    listed = await client.get("/api/v1/tenants", headers=admin)
    assert [t["name"] for t in listed.json()] == ["acme", "globex"]
    assert all(t["status"] == "active" for t in listed.json())


async def test_duplicate_tenant_name_is_409(client, admin, make_tenant):
    await make_tenant("acme")
    response = await client.post("/api/v1/tenants", json={"name": "acme"}, headers=admin)
    assert response.status_code == 409


async def test_blank_tenant_name_is_rejected(client, admin):
    response = await client.post("/api/v1/tenants", json={"name": ""}, headers=admin)
    assert response.status_code == 422


async def test_create_plan_and_reject_invalid_limits(client, admin):
    ok = await client.post(
        "/api/v1/plans",
        json={"name": "Basic", "requests_per_window": 60, "window_seconds": 60},
        headers=admin,
    )
    assert ok.status_code == 201
    assert ok.json()["monthly_quota"] is None

    for bad in (
        {"name": "Zero", "requests_per_window": 0, "window_seconds": 60},
        {"name": "NoWindow", "requests_per_window": 10, "window_seconds": 0},
        {"name": "NegQuota", "requests_per_window": 10, "window_seconds": 60, "monthly_quota": -1},
    ):
        response = await client.post("/api/v1/plans", json=bad, headers=admin)
        assert response.status_code == 422, bad


async def test_duplicate_plan_name_is_409(client, admin):
    body = {"name": "Basic", "requests_per_window": 60, "window_seconds": 60}
    assert (await client.post("/api/v1/plans", json=body, headers=admin)).status_code == 201
    assert (await client.post("/api/v1/plans", json=body, headers=admin)).status_code == 409


async def _plan(client, admin, name, limit):
    response = await client.post(
        "/api/v1/plans",
        json={"name": name, "requests_per_window": limit, "window_seconds": 60},
        headers=admin,
    )
    return response.json()


async def test_assign_plan_and_change_it_keeps_history(client, admin, db, make_tenant):
    tenant = await make_tenant()
    basic = await _plan(client, admin, "Basic", 60)
    pro = await _plan(client, admin, "Pro", 300)
    url = f"/api/v1/tenants/{tenant['id']}/plan"

    first = await client.put(url, json={"plan_id": basic["id"]}, headers=admin)
    assert first.status_code == 200
    assert first.json()["plan"]["name"] == "Basic"
    assert first.json()["effective_until"] is None

    second = await client.put(url, json={"plan_id": pro["id"]}, headers=admin)
    assert second.json()["plan"]["name"] == "Pro"

    rows = (
        (await db.execute(select(TenantPlan).order_by(TenantPlan.effective_from))).scalars().all()
    )
    assert len(rows) == 2
    assert rows[0].effective_until is not None  # old assignment closed, not deleted
    assert rows[1].effective_until is None

    import uuid

    active = await tenant_repo.get_active_tenant_plan(
        db, uuid.UUID(tenant["id"]), datetime.now(UTC)
    )
    assert active.plan.name == "Pro"


async def test_assigning_same_plan_twice_is_a_no_op(client, admin, db, make_tenant):
    tenant = await make_tenant()
    basic = await _plan(client, admin, "Basic", 60)
    url = f"/api/v1/tenants/{tenant['id']}/plan"
    await client.put(url, json={"plan_id": basic["id"]}, headers=admin)
    await client.put(url, json={"plan_id": basic["id"]}, headers=admin)
    rows = (await db.execute(select(TenantPlan))).scalars().all()
    assert len(rows) == 1


async def test_assign_plan_unknown_tenant_or_plan_is_404(client, admin, make_tenant):
    tenant = await make_tenant()
    missing = "00000000-0000-0000-0000-000000000000"
    r1 = await client.put(
        f"/api/v1/tenants/{missing}/plan", json={"plan_id": missing}, headers=admin
    )
    assert r1.status_code == 404
    r2 = await client.put(
        f"/api/v1/tenants/{tenant['id']}/plan", json={"plan_id": missing}, headers=admin
    )
    assert r2.status_code == 404
