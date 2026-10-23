"""The limiter against a real Redis: the behaviour that makes distributed limiting correct."""

import asyncio
import uuid
from datetime import UTC, datetime

from app.core.redis import create_redis_client
from app.services.policy import ResolvedPolicy
from app.services.rate_limiter import RateLimiter

OCT = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)
NOV = datetime(2026, 11, 2, 12, 0, 0, tzinfo=UTC)


def plan(limit: int, window: int = 3600, quota: int | None = None) -> ResolvedPolicy:
    return ResolvedPolicy("test", limit, window, quota)


async def test_burst_up_to_capacity_then_reject(redis_client):
    limiter, tenant = RateLimiter(redis_client), uuid.uuid4()
    results = [await limiter.check(tenant, plan(5)) for _ in range(5)]
    assert all(r.allowed for r in results)
    assert [r.remaining for r in results] == [4, 3, 2, 1, 0]

    rejected = await limiter.check(tenant, plan(5))
    assert rejected.outcome == "rate_limited"
    assert rejected.remaining == 0
    assert rejected.limit == 5
    assert 700 <= rejected.retry_after_seconds <= 720  # one token per 720s at 5 per hour


async def test_reset_seconds_reports_time_until_full(redis_client):
    limiter = RateLimiter(redis_client)
    first = await limiter.check(uuid.uuid4(), plan(5))  # 4 of 5 left: one token (720s) to refill
    assert 719 <= first.reset_seconds <= 720


async def test_tokens_refill_over_time(redis_client):
    limiter, tenant = RateLimiter(redis_client), uuid.uuid4()
    # 2 tokens per second: slow enough that a sluggish machine cannot refill the bucket
    # between the draining calls and the first rejection.
    slow = plan(10, window=5)
    for _ in range(10):
        assert (await limiter.check(tenant, slow)).allowed
    assert not (await limiter.check(tenant, slow)).allowed

    await asyncio.sleep(1.1)  # about 2 tokens
    allowed = 0
    while (await limiter.check(tenant, slow)).allowed:
        allowed += 1
        assert allowed < 6, "bucket refilled past what the elapsed time allows"
    assert 1 <= allowed <= 4


async def test_idle_bucket_never_exceeds_capacity(redis_client):
    limiter, tenant = RateLimiter(redis_client), uuid.uuid4()
    quick = plan(3, window=1)
    assert (await limiter.check(tenant, quick)).remaining == 2
    await asyncio.sleep(1.5)  # far longer than needed to refill
    assert (await limiter.check(tenant, quick)).remaining == 2  # capped at 3, minus this one


async def test_200_concurrent_requests_allow_exactly_capacity(redis_client):
    limiter, tenant = RateLimiter(redis_client), uuid.uuid4()
    results = await asyncio.gather(*(limiter.check(tenant, plan(50)) for _ in range(200)))
    assert sum(r.allowed for r in results) == 50
    assert sum(r.outcome == "rate_limited" for r in results) == 150


async def test_two_gateway_instances_share_one_limit(redis_client, redis_url):
    """Two separate limiters on separate connections, as two gateway processes would have."""
    other_client = create_redis_client(redis_url, max_connections=100, timeout_seconds=2)
    try:
        instances = [RateLimiter(redis_client), RateLimiter(other_client)]
        tenant = uuid.uuid4()
        results = await asyncio.gather(
            *(instances[i % 2].check(tenant, plan(30)) for i in range(120))
        )
        assert (
            sum(r.allowed for r in results) == 30
        )  # not 60, which per-process counters would give
    finally:
        await other_client.aclose()


async def test_tenants_do_not_affect_each_other(redis_client):
    limiter, a, b = RateLimiter(redis_client), uuid.uuid4(), uuid.uuid4()
    for _ in range(3):
        await limiter.check(a, plan(3))
    assert (await limiter.check(a, plan(3))).outcome == "rate_limited"
    assert (await limiter.check(b, plan(3))).allowed


async def test_fractional_refill_rates_work(redis_client):
    limiter, tenant = RateLimiter(redis_client), uuid.uuid4()
    slow = plan(1, window=86_400)  # one request per day
    assert (await limiter.check(tenant, slow)).allowed
    second = await limiter.check(tenant, slow)
    assert second.outcome == "rate_limited"
    assert 86_000 <= second.retry_after_seconds <= 86_400


async def test_lowering_a_plan_clamps_the_existing_bucket(redis_client):
    limiter, tenant = RateLimiter(redis_client), uuid.uuid4()
    await limiter.check(tenant, plan(100))  # bucket holds 99
    after_downgrade = await limiter.check(tenant, plan(5))
    assert after_downgrade.remaining == 4  # clamped to the new capacity, then one spent


async def test_raising_a_plan_gives_the_new_capacity_gradually(redis_client):
    limiter, tenant = RateLimiter(redis_client), uuid.uuid4()
    slow = plan(2, window=10)  # one token per 5 seconds
    for _ in range(2):
        await limiter.check(tenant, slow)
    assert (await limiter.check(tenant, slow)).outcome == "rate_limited"
    upgraded = plan(100, window=1)  # 100 tokens/second
    await asyncio.sleep(0.1)
    assert (await limiter.check(tenant, upgraded)).allowed


async def test_script_is_reloaded_after_redis_forgets_it(redis_client):
    limiter, tenant = RateLimiter(redis_client), uuid.uuid4()
    assert (await limiter.check(tenant, plan(5))).allowed
    await redis_client.script_flush()  # what a Redis restart does to cached scripts
    assert (await limiter.check(tenant, plan(5))).allowed


async def test_idle_keys_expire_so_redis_does_not_fill_up(redis_client):
    limiter, tenant = RateLimiter(redis_client), uuid.uuid4()
    await limiter.check(tenant, plan(5, quota=10), now=OCT)
    bucket_ttl_ms = await redis_client.pttl(f"rl:{tenant}")
    assert 0 < bucket_ttl_ms <= 3_601_000  # time to refill fully, plus a second
    quota_ttl = await redis_client.ttl(f"quota:{tenant}:202610")
    assert 0 < quota_ttl <= 11 * 86400 + 12 * 3600 + 3 * 86400


# ---------------- monthly quota ----------------
async def test_monthly_quota_is_enforced(redis_client):
    limiter, tenant = RateLimiter(redis_client), uuid.uuid4()
    policy = plan(1000, window=60, quota=5)
    results = [await limiter.check(tenant, policy, now=OCT) for _ in range(5)]
    assert all(r.allowed for r in results)
    assert [r.quota_remaining for r in results] == [4, 3, 2, 1, 0]

    over = await limiter.check(tenant, policy, now=OCT)
    assert over.outcome == "quota_exceeded"
    assert over.quota_limit == 5 and over.quota_remaining == 0
    assert over.retry_after_seconds == over.quota_reset_seconds == 11 * 86400 + 12 * 3600


async def test_quota_starts_fresh_in_a_new_month(redis_client):
    limiter, tenant = RateLimiter(redis_client), uuid.uuid4()
    policy = plan(1000, window=60, quota=1)
    assert (await limiter.check(tenant, policy, now=OCT)).allowed
    assert (await limiter.check(tenant, policy, now=OCT)).outcome == "quota_exceeded"
    assert (await limiter.check(tenant, policy, now=NOV)).allowed


async def test_rate_limited_requests_do_not_use_up_quota(redis_client):
    limiter, tenant = RateLimiter(redis_client), uuid.uuid4()
    policy = plan(2, quota=100)
    results = [await limiter.check(tenant, policy, now=OCT) for _ in range(6)]
    assert sum(r.allowed for r in results) == 2
    assert await redis_client.get(f"quota:{tenant}:202610") == "2"


async def test_quota_rejections_do_not_spend_tokens(redis_client):
    limiter, tenant = RateLimiter(redis_client), uuid.uuid4()
    policy = plan(10, quota=3)
    for _ in range(3):
        assert (await limiter.check(tenant, policy, now=OCT)).allowed
    for _ in range(5):
        assert (await limiter.check(tenant, policy, now=OCT)).outcome == "quota_exceeded"
    tokens = float(await redis_client.hget(f"rl:{tenant}", "tokens"))
    assert 6.9 <= tokens <= 7.1  # 10 - 3 spent; the five rejections cost nothing


async def test_quota_is_exact_under_concurrency(redis_client):
    limiter, tenant = RateLimiter(redis_client), uuid.uuid4()
    policy = plan(10_000, window=60, quota=40)
    results = await asyncio.gather(*(limiter.check(tenant, policy, now=OCT) for _ in range(150)))
    assert sum(r.allowed for r in results) == 40
    assert sum(r.outcome == "quota_exceeded" for r in results) == 110


async def test_no_quota_means_no_quota_keys(redis_client):
    limiter, tenant = RateLimiter(redis_client), uuid.uuid4()
    result = await limiter.check(tenant, plan(5), now=OCT)
    assert result.quota_limit is None and result.quota_remaining is None
    assert await redis_client.exists(f"quota:{tenant}:202610") == 0
