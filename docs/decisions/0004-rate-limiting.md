# 0004: Rate limiting and quotas

Status: accepted (Phase 4)

## Algorithm: token bucket

A bucket holds up to `requests_per_window` tokens and refills continuously at
`requests_per_window / window_seconds` per second. A request spends one token; with none left
it is rejected. Compared with a fixed window this allows short bursts up to the capacity while
holding the long-run average to the plan, and it avoids the "double burst" at window edges
(a full allowance at the end of one window and again at the start of the next).

## Atomicity: one Lua script

The check reads the bucket, refills it, decides, and writes it back. Done as separate Redis
commands from Python, concurrent requests interleave. Measured on the same machine, 200
concurrent requests against a limit of 50:

| Implementation | Allowed |
|---|---|
| Read-then-write from Python | 200 |
| Lua script (this project) | 50 |

The script runs atomically inside Redis, so it is correct for any number of gateway instances.
It uses Redis `TIME`, not the gateway's clock, so instances with drifting clocks agree on refill.
`register_script` reloads the script automatically if Redis restarts and forgets it.

## One script, two limits

The short-window bucket and the optional monthly quota are evaluated in the same script call:

- A request rejected for the short-window limit does **not** use up monthly quota.
- A request rejected for the monthly quota does **not** spend a token.
- Quota is a counter in `quota:<tenant>:<YYYYMM>`; it starts fresh each UTC month and expires
  3 days after the month ends.

**Limitation:** the quota is a Redis counter. It is accurate under concurrency (tested) but it is
not billing-grade accounting: if Redis loses data (restart without persistence, eviction), the
counter resets. Do not describe it as exact metering.

## What counts

Only requests that would actually be forwarded are checked: authenticated, route found, method
allowed, path valid. Unknown routes (404), disallowed methods (405) and malformed paths (400)
are rejected earlier and cost no token or quota. Requests that fail upstream (502/504) *do*
count, because the upstream may have done work.

## Response contract

- Allowed: `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset` (seconds until the
  bucket is full), plus `X-Quota-Limit` / `X-Quota-Remaining` when the plan has a quota. These
  replace any same-named headers from the upstream.
- Rejected: HTTP 429 with a JSON `detail`, the same headers, and `Retry-After` in whole seconds
  (the time until one token is available, or until the next month for quota).
- No plan assigned: 403. The gateway refuses rather than inventing a default limit.

## When Redis is unavailable

Configurable with `RATE_LIMIT_FAILURE_MODE`:

- `closed` (default): respond 503 with `Retry-After: 1`. Upstreams stay protected; the gateway
  is unavailable while Redis is.
- `open`: forward without limiting. The gateway stays available; limits are not enforced and
  quotas are not counted during the outage.

Default is closed because a rate limiter that silently stops limiting is a quiet failure, while
a 503 is loud and recoverable. Choose open if availability matters more than enforcement.
Redis calls time out after `REDIS_TIMEOUT_SECONDS` (0.5), so a hung Redis fails fast.
While Redis is down, every request still pays that timeout; a circuit breaker that skips the
check for a few seconds after a failure is a sensible later improvement.

## Redis connection pool

The default redis-py pool raises an error as soon as all connections are busy. Under 200
concurrent requests this produced `MaxConnectionsError`, which the gateway would have reported as
a Redis outage. The gateway uses a bounded *blocking* pool (`REDIS_MAX_CONNECTIONS`, default
100): a request waits briefly for a free connection instead of failing.

## Plan lookup and caching

Each gateway process caches a tenant's resolved plan for `POLICY_CACHE_TTL_SECONDS` (30), so the
limiter does not query the plan on every request.

- A plan change made through the same process takes effect immediately (the entry is dropped).
- A change made through another instance, or directly in the database, is noticed within the TTL.
- "No plan" is cached as well.

When a plan changes, the existing bucket is kept: a lower limit clamps the stored tokens to the
new capacity at once; a higher limit fills at the new, faster rate rather than instantly.

## Not done (by design)

- Limits are per tenant, not per route or per API key.
- No separate limiter for unauthenticated abuse; authentication itself is cheap but not free.
- Redis persistence settings, Sentinel/Cluster and key-eviction policy are deployment concerns.
