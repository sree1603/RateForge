import asyncio

import httpx
from sqlalchemy import delete

from app.core.config import get_settings
from app.core.redis import create_redis_client
from app.main import app
from app.models import TenantPlan
from app.services.policy import policy_cache
from app.services.rate_limiter import RateLimiter


async def hit(client, ctx, path="/gateway/echo/x", **kwargs):
    return await client.get(path, headers=ctx["headers"], **kwargs)


# ---------------- basic limiting ----------------
async def test_requests_over_the_limit_get_429_and_never_reach_the_upstream(
    client, gateway, upstream
):
    ctx = await gateway(limit=3, window=3600)
    statuses = [(await hit(client, ctx)).status_code for _ in range(5)]
    assert statuses == [200, 200, 200, 429, 429]
    assert len(upstream.calls) == 3


async def test_rate_limit_headers_on_success(client, gateway):
    ctx = await gateway(limit=3, window=3600)
    seen = []
    for _ in range(3):
        response = await hit(client, ctx)
        seen.append(
            (response.headers["x-ratelimit-limit"], response.headers["x-ratelimit-remaining"])
        )
        assert int(response.headers["x-ratelimit-reset"]) > 0
    assert seen == [("3", "2"), ("3", "1"), ("3", "0")]


async def test_429_response_explains_itself(client, gateway):
    ctx = await gateway(limit=1, window=3600)
    await hit(client, ctx)
    response = await hit(client, ctx)
    assert response.status_code == 429
    assert response.json() == {"detail": "Rate limit exceeded"}
    assert response.headers["x-ratelimit-limit"] == "1"
    assert response.headers["x-ratelimit-remaining"] == "0"
    retry_after = int(response.headers["retry-after"])
    assert 1 <= retry_after <= 3600
    assert response.headers["x-request-id"].startswith("req_")


async def test_retry_after_is_honest(client, gateway):
    """Waiting for as long as Retry-After says must be enough to get through.

    The bucket refills slowly (1 token per 3 seconds) so that a slow machine cannot refill
    it between the two requests and make the first rejection disappear.
    """
    ctx = await gateway(limit=1, window=3)
    assert (await hit(client, ctx)).status_code == 200
    rejected = await hit(client, ctx)
    assert rejected.status_code == 429
    retry_after = int(rejected.headers["retry-after"])
    assert 1 <= retry_after <= 3
    await asyncio.sleep(retry_after + 0.1)
    assert (await hit(client, ctx)).status_code == 200


async def test_gateway_headers_replace_any_the_upstream_sends(client, gateway, upstream):
    ctx = await gateway(limit=10, window=3600)
    upstream.responder = lambda r: httpx.Response(
        200, json={}, headers={"X-RateLimit-Remaining": "99999", "X-RateLimit-Limit": "1"}
    )
    response = await hit(client, ctx)
    assert response.headers["x-ratelimit-limit"] == "10"
    assert response.headers["x-ratelimit-remaining"] == "9"
    assert response.headers.get_list("x-ratelimit-remaining") == ["9"]


async def test_upstream_errors_still_count_and_carry_headers(client, gateway, upstream):
    ctx = await gateway(limit=3, window=3600)
    upstream.responder = lambda r: httpx.ConnectError("down")
    first = await hit(client, ctx)
    assert first.status_code == 502
    assert first.headers["x-ratelimit-remaining"] == "2"  # the attempt used a token


# ---------------- who gets limited ----------------
async def test_each_tenant_has_its_own_bucket(client, gateway):
    ctx = await gateway(limit=2, window=3600)
    other = await ctx["add_tenant"]("globex", limit=2, window=3600)
    for _ in range(2):
        await hit(client, ctx)
    assert (await hit(client, ctx)).status_code == 429
    assert (await hit(client, other)).status_code == 200


async def test_different_plans_give_different_limits(client, gateway):
    ctx = await gateway(limit=2, window=3600)
    pro = await ctx["add_tenant"]("pro-customer", limit=5, window=3600)
    basic_ok = [(await hit(client, ctx)).status_code for _ in range(4)]
    pro_ok = [(await hit(client, pro)).status_code for _ in range(6)]
    assert basic_ok == [200, 200, 429, 429]
    assert pro_ok == [200] * 5 + [429]


async def test_tenant_without_a_plan_is_refused(client, gateway, upstream):
    ctx = await gateway(limit=None)
    response = await hit(client, ctx)
    assert response.status_code == 403
    assert "plan" in response.json()["detail"].lower()
    assert upstream.calls == []


async def test_requests_that_would_not_be_forwarded_cost_nothing(client, gateway, upstream):
    ctx = await gateway(limit=2, window=3600, methods=("GET",))
    assert (await hit(client, ctx, "/gateway/unknown/x")).status_code == 404
    assert (await client.delete("/gateway/echo/x", headers=ctx["headers"])).status_code == 405
    assert (await hit(client, ctx, "/gateway/echo/%2e%2e/x")).status_code == 400
    # All three were rejected before the limiter, so both tokens are still there.
    assert [(await hit(client, ctx)).status_code for _ in range(3)] == [200, 200, 429]
    assert len(upstream.calls) == 2


async def test_unauthenticated_requests_are_not_counted_against_anyone(client, gateway):
    ctx = await gateway(limit=1, window=3600)
    for _ in range(5):
        assert (await client.get("/gateway/echo/x")).status_code == 401
    assert (await hit(client, ctx)).status_code == 200


# ---------------- concurrency ----------------
async def test_concurrent_requests_through_the_gateway_respect_the_limit(client, gateway, upstream):
    ctx = await gateway(limit=15, window=3600)
    responses = await asyncio.gather(*(hit(client, ctx, f"/gateway/echo/c{i}") for i in range(40)))
    statuses = [r.status_code for r in responses]
    assert statuses.count(200) == 15
    assert statuses.count(429) == 25
    assert len(upstream.calls) == 15


# ---------------- plans and the policy cache ----------------
async def test_changing_a_plan_takes_effect_in_this_process_immediately(client, gateway):
    ctx = await gateway(limit=1, window=3600)
    assert (await hit(client, ctx)).status_code == 200
    assert (await hit(client, ctx)).status_code == 429

    await ctx["assign_plan"](ctx["tenant"]["id"], 1000, 1, None)  # 1000 tokens per second
    await asyncio.sleep(0.1)
    assert (await hit(client, ctx)).status_code == 200


async def test_plan_is_cached_so_changes_elsewhere_apply_after_the_ttl(client, gateway, db):
    """Documents the trade-off: a change made behind this process's back is not seen
    until the cached entry expires."""
    ctx = await gateway(limit=100, window=3600)
    assert (await hit(client, ctx)).status_code == 200  # populates the cache

    await db.execute(delete(TenantPlan))  # e.g. another instance or an admin script did this
    await db.commit()
    assert (await hit(client, ctx)).status_code == 200  # still served from the cached policy

    policy_cache.clear()  # same as the TTL passing
    assert (await hit(client, ctx)).status_code == 403


# ---------------- monthly quota ----------------
async def test_monthly_quota_returns_429_with_quota_headers(client, gateway, upstream):
    ctx = await gateway(limit=1000, window=60, quota=2)
    first = await hit(client, ctx)
    assert first.headers["x-quota-limit"] == "2"
    assert first.headers["x-quota-remaining"] == "1"
    assert (await hit(client, ctx)).headers["x-quota-remaining"] == "0"

    over = await hit(client, ctx)
    assert over.status_code == 429
    assert over.json() == {"detail": "Monthly quota exceeded"}
    assert over.headers["x-quota-remaining"] == "0"
    assert int(over.headers["retry-after"]) > 0
    assert len(upstream.calls) == 2


async def test_per_minute_limit_and_monthly_quota_are_independent(client, gateway):
    ctx = await gateway(limit=2, window=3600, quota=1000)
    statuses = [(await hit(client, ctx)).status_code for _ in range(3)]
    assert statuses == [200, 200, 429]  # under quota, but over the short-window limit
    third = await hit(client, ctx)
    assert third.json() == {"detail": "Rate limit exceeded"}
    assert third.headers["x-quota-remaining"] == "998"  # the rejected requests cost no quota


# ---------------- Redis unavailable ----------------
def _broken_limiter() -> RateLimiter:
    # Nothing listens on port 1: a real connection failure, not a simulated one.
    return RateLimiter(
        create_redis_client("redis://127.0.0.1:1/0", max_connections=2, timeout_seconds=0.2)
    )


async def test_redis_down_fails_closed_by_default(client, gateway, upstream):
    ctx = await gateway()
    app.state.rate_limiter = _broken_limiter()
    response = await hit(client, ctx)
    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert "x-ratelimit-limit" not in response.headers
    assert upstream.calls == []


async def test_redis_down_can_fail_open(client, gateway, upstream, monkeypatch):
    ctx = await gateway()
    monkeypatch.setattr(get_settings(), "rate_limit_failure_mode", "open")
    app.state.rate_limiter = _broken_limiter()
    response = await hit(client, ctx)
    assert response.status_code == 200
    assert "x-ratelimit-limit" not in response.headers  # no limit info to report
    assert len(upstream.calls) == 1


async def test_gateway_recovers_when_redis_comes_back(client, gateway, redis_client):
    ctx = await gateway(limit=5, window=3600)
    healthy = app.state.rate_limiter
    app.state.rate_limiter = _broken_limiter()
    assert (await hit(client, ctx)).status_code == 503
    app.state.rate_limiter = healthy
    assert (await hit(client, ctx)).status_code == 200
