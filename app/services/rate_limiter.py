"""Distributed token-bucket rate limiter with an optional monthly quota, backed by Redis.

Why a Lua script: the check has to read the bucket, refill it, decide, and write it back.
Done from Python as separate commands, two gateway instances can both read "1 token left"
and both spend it. A Lua script runs atomically inside Redis, so the whole sequence is one
indivisible step no matter how many instances are calling.

Why Redis TIME: the refill maths uses the Redis server's clock, so gateway instances whose
own clocks disagree by a few hundred milliseconds still agree about how much has refilled.
"""

import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from redis.asyncio import Redis

from app.services.policy import ResolvedPolicy

# KEYS[1] bucket hash, KEYS[2] monthly quota counter
# ARGV[1] capacity, ARGV[2] refill tokens per millisecond, ARGV[3] cost,
# ARGV[4] monthly quota (0 = none), ARGV[5] quota key TTL in seconds
# Returns {status, remaining_tokens, retry_after_ms, quota_used, tokens_milli}
#   status: 1 allowed, 0 rate limited, 2 monthly quota exceeded
TOKEN_BUCKET_LUA = """
local capacity = tonumber(ARGV[1])
local refill_per_ms = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local quota = tonumber(ARGV[4])
local quota_ttl = tonumber(ARGV[5])

local t = redis.call('TIME')
local now_ms = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)

local quota_used = 0
if quota > 0 then
  quota_used = tonumber(redis.call('GET', KEYS[2]) or '0')
  if quota_used >= quota then
    return {2, 0, 0, quota_used, 0}
  end
end

local data = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil or ts == nil then
  tokens = capacity
  ts = now_ms
end

local elapsed = now_ms - ts
if elapsed < 0 then elapsed = 0 end
tokens = math.min(capacity, tokens + elapsed * refill_per_ms)

local status = 0
local retry_ms = 0
if tokens >= cost then
  tokens = tokens - cost
  status = 1
else
  retry_ms = math.ceil((cost - tokens) / refill_per_ms)
end

redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'ts', tostring(now_ms))
-- An idle bucket refills to full anyway, so it can be forgotten after that long.
redis.call('PEXPIRE', KEYS[1], math.ceil(capacity / refill_per_ms) + 1000)

if status == 1 and quota > 0 then
  quota_used = redis.call('INCR', KEYS[2])
  if quota_used == 1 then
    redis.call('EXPIRE', KEYS[2], quota_ttl)
  end
end

return {status, math.floor(tokens), retry_ms, quota_used, math.floor(tokens * 1000)}
"""

QUOTA_TTL_MARGIN_SECONDS = 3 * 86400


@dataclass(frozen=True)
class Decision:
    outcome: Literal["allowed", "rate_limited", "quota_exceeded"]
    limit: int
    remaining: int
    retry_after_seconds: int  # 0 when allowed
    reset_seconds: int  # until the bucket is full again
    quota_limit: int | None = None
    quota_remaining: int | None = None
    quota_reset_seconds: int | None = None  # until the next UTC month starts

    @property
    def allowed(self) -> bool:
        return self.outcome == "allowed"


def _month_info(now: datetime) -> tuple[str, int]:
    """(YYYYMM label, whole seconds until the next UTC month begins)."""
    if now.month == 12:
        next_month = now.replace(
            year=now.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0
        )
    else:
        next_month = now.replace(
            month=now.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0
        )
    return now.strftime("%Y%m"), max(1, math.ceil((next_month - now).total_seconds()))


class RateLimiter:
    def __init__(self, redis: Redis) -> None:
        # register_script reloads the script transparently if Redis restarts and forgets it.
        self._script = redis.register_script(TOKEN_BUCKET_LUA)

    async def check(
        self,
        tenant_id: uuid.UUID | str,
        policy: ResolvedPolicy,
        *,
        now: datetime | None = None,
        cost: int = 1,
    ) -> Decision:
        """Atomically try to spend `cost` tokens for the tenant. May raise redis.RedisError."""
        now = now or datetime.now(UTC)
        month_label, seconds_to_month_end = _month_info(now)
        capacity = policy.requests_per_window
        refill_per_ms = capacity / (policy.window_seconds * 1000)
        quota = policy.monthly_quota or 0

        status, remaining, retry_ms, quota_used, tokens_milli = await self._script(
            keys=[f"rl:{tenant_id}", f"quota:{tenant_id}:{month_label}"],
            args=[
                capacity,
                repr(refill_per_ms),
                cost,
                quota,
                seconds_to_month_end + QUOTA_TTL_MARGIN_SECONDS,
            ],
        )

        missing = capacity - tokens_milli / 1000
        reset_seconds = (
            max(0, math.ceil(missing * policy.window_seconds / capacity)) if status != 2 else 0
        )
        quota_fields: dict = {}
        if quota:
            quota_fields = {
                "quota_limit": quota,
                "quota_remaining": max(0, quota - quota_used),
                "quota_reset_seconds": seconds_to_month_end,
            }
        if status == 2:
            return Decision("quota_exceeded", capacity, 0, seconds_to_month_end, 0, **quota_fields)
        if status == 1:
            return Decision("allowed", capacity, remaining, 0, reset_seconds, **quota_fields)
        return Decision(
            "rate_limited",
            capacity,
            remaining,
            max(1, math.ceil(retry_ms / 1000)),
            reset_seconds,
            **quota_fields,
        )
