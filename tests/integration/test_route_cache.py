"""The route cache: faster lookups, with a documented and bounded delay for outside changes."""

from sqlalchemy import delete, update

from app.core.config import get_settings
from app.models import Route
from app.services import routing as routing_module
from app.services.routing import route_cache


async def hit(client, ctx, path="/gateway/echo/x"):
    return await client.get(path, headers=ctx["headers"])


def count_database_route_lookups(monkeypatch) -> list[str]:
    calls: list[str] = []
    original = routing_module.routing_repo.get_route_by_prefix

    async def counting(session, prefix):
        calls.append(prefix)
        return await original(session, prefix)

    monkeypatch.setattr(routing_module.routing_repo, "get_route_by_prefix", counting)
    return calls


async def test_repeated_requests_look_the_route_up_once(client, gateway, monkeypatch):
    ctx = await gateway()
    lookups = count_database_route_lookups(monkeypatch)
    for _ in range(10):
        assert (await hit(client, ctx)).status_code == 200
    assert lookups == ["echo"]  # one database query, nine cache hits


async def test_unknown_prefixes_are_remembered_so_probing_is_cheap(client, gateway, monkeypatch):
    ctx = await gateway()
    lookups = count_database_route_lookups(monkeypatch)
    for _ in range(5):
        assert (await hit(client, ctx, "/gateway/not-a-route/x")).status_code == 404
    assert lookups == ["not-a-route"]


async def test_changes_made_through_the_api_take_effect_immediately(client, gateway, admin):
    ctx = await gateway()
    assert (await hit(client, ctx)).status_code == 200  # cached
    route_id, upstream_id = ctx["route"]["id"], ctx["upstream"]["id"]

    await client.patch(f"/api/v1/routes/{route_id}", json={"enabled": False}, headers=admin)
    assert (await hit(client, ctx)).status_code == 404
    await client.patch(f"/api/v1/routes/{route_id}", json={"enabled": True}, headers=admin)
    assert (await hit(client, ctx)).status_code == 200
    await client.patch(f"/api/v1/upstreams/{upstream_id}", json={"enabled": False}, headers=admin)
    assert (await hit(client, ctx)).status_code == 404


async def test_a_new_route_works_at_once_even_after_its_prefix_was_cached_as_unknown(
    client, gateway, admin
):
    ctx = await gateway()
    assert (await hit(client, ctx, "/gateway/later/x")).status_code == 404  # cached as unknown
    created = await client.post(
        "/api/v1/routes",
        json={"upstream_id": ctx["upstream"]["id"], "path_prefix": "later"},
        headers=admin,
    )
    assert created.status_code == 201
    assert (await hit(client, ctx, "/gateway/later/x")).status_code == 200


async def test_changes_made_behind_the_cache_are_seen_only_after_expiry(client, gateway, db):
    """Documents the trade-off: an edit made outside this process (another instance, a direct
    database change) is noticed when the cached entry expires, not before."""
    ctx = await gateway()
    assert (await hit(client, ctx)).status_code == 200

    await db.execute(update(Route).values(enabled=False))
    await db.commit()
    assert (await hit(client, ctx)).status_code == 200  # still served from the cache

    route_cache.clear()  # what the entry expiring does
    assert (await hit(client, ctx)).status_code == 404


async def test_entries_expire_on_their_own(client, gateway, db, monkeypatch):
    import time

    ctx = await gateway()
    await hit(client, ctx)
    await db.execute(delete(Route))
    await db.commit()
    assert (await hit(client, ctx)).status_code == 200

    real = time.monotonic
    monkeypatch.setattr(routing_module.time, "monotonic", lambda: real() + 3600)
    assert (await hit(client, ctx)).status_code == 404


async def test_the_cache_can_be_switched_off(client, gateway, db, monkeypatch):
    monkeypatch.setattr(get_settings(), "route_cache_ttl_seconds", 0)
    ctx = await gateway()
    lookups = count_database_route_lookups(monkeypatch)
    await hit(client, ctx)
    await hit(client, ctx)
    assert lookups == ["echo", "echo"]  # every request reads the database again

    await db.execute(delete(Route))
    await db.commit()
    assert (await hit(client, ctx)).status_code == 404  # no delay at all


async def test_cached_routes_do_not_leak_between_prefixes(client, gateway, admin, upstream):
    ctx = await gateway()
    other = await client.post(
        "/api/v1/upstreams",
        json={"name": "second", "base_url": "http://second.test:7000"},
        headers=admin,
    )
    await client.post(
        "/api/v1/routes",
        json={"upstream_id": other.json()["id"], "path_prefix": "second"},
        headers=admin,
    )
    await hit(client, ctx, "/gateway/echo/a")
    await hit(client, ctx, "/gateway/second/b")
    hosts = [(c.request.url.host, c.request.url.port) for c in upstream.calls]
    assert hosts == [("upstream.test", 9000), ("second.test", 7000)]
