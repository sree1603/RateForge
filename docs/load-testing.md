# Load testing

How to measure RateForge on your own machine, and what was learned from measuring it. The recorded
results are in [performance.md](performance.md). **Numbers are only meaningful with the machine
and settings they came from**, so every run records them.

## What is measured

Four scenarios, driven by [Locust](https://locust.io) (`tests/load/locustfile.py`):

| Scenario | What it sends | What it tells you |
|---|---|---|
| `baseline` | Requests straight to the upstream | What the upstream alone can do. The gateway cannot beat it. |
| `allowed` | One tenant with a huge limit, every request forwarded | The gateway's cost on the normal path |
| `limited` | One tenant with an empty bucket, almost every request is a 429 | The cost of *rejecting*: auth + Redis, no upstream call |
| `mixed` | 20 tenants with modest limits | A realistic blend of 200s and 429s |

The gateway's overhead is the gap between `baseline` and `allowed`. Users send requests back to back
with no pauses, so results are close to a maximum, not a prediction of everyday traffic.

For each scenario the report gives requests per second and p50 / p95 / p99 latency (the time within
which 50%, 95% and 99% of requests finished), the share of each status code, and unexpected failures.

## Running it (Docker Compose, PowerShell)

Use the production-style override: no auto-reload, no per-request access log, warnings-only logging,
and settings you can vary one at a time.

```powershell
# 1. Start the stack in load-test mode (1 worker, 1 instance).
docker compose down
docker compose -f compose.yaml -f compose.load.yaml up -d --build

# 2. Run the suite. Describe your machine honestly: CPU model, cores, RAM, Windows + Docker Desktop.
docker compose -f compose.yaml -f compose.load.yaml run --rm api python scripts/run_load_suite.py `
  --label "1 worker, 1 instance" `
  --machine "<CPU model, cores, RAM; Windows 11, Docker Desktop (WSL2)>"
```

Results are written to `docs/performance-results/<label>/` (raw Locust CSVs) and
`docs/performance.md` is regenerated. Commit both: they are your evidence.

Useful options: `--users 50` (concurrent users), `--duration 30` (seconds per scenario),
`--scenarios allowed,mixed` (a subset), `--note "..."` (anything else you changed).

### Change one thing at a time

Re-create the stack with a different setting, then run the suite again with a new `--label`.

```powershell
# More server processes per instance
$env:WORKERS = "2"; docker compose -f compose.yaml -f compose.load.yaml up -d --force-recreate
docker compose -f compose.yaml -f compose.load.yaml run --rm api python scripts/run_load_suite.py --label "2 workers" --machine "..."
Remove-Item Env:WORKERS

# Two gateway instances (api and api2) sharing Redis and PostgreSQL
docker compose -f compose.yaml -f compose.load.yaml run --rm api python scripts/run_load_suite.py `
  --label "2 instances" --machine "..." --gateway-urls "http://api:8000,http://api2:8000"

# Usage recording off (how much does analytics cost?)
$env:USAGE_RECORDING_ENABLED = "false"; docker compose -f compose.yaml -f compose.load.yaml up -d --force-recreate
docker compose -f compose.yaml -f compose.load.yaml run --rm api python scripts/run_load_suite.py --label "usage recording off" --machine "..."
Remove-Item Env:USAGE_RECORDING_ENABLED

# Route cache off (see the effect of caching route lookups)
$env:ROUTE_CACHE_TTL_SECONDS = "0"; docker compose -f compose.yaml -f compose.load.yaml up -d --force-recreate
docker compose -f compose.yaml -f compose.load.yaml run --rm api python scripts/run_load_suite.py --label "route cache off" --machine "..."
Remove-Item Env:ROUTE_CACHE_TTL_SECONDS
```

Notes:

- Run each configuration twice if you can; the spread between runs tells you how much to trust a
  difference.
- Close other heavy programs, and leave the laptop plugged in.
- Load tests fill `usage_events`. Empty it between experiments if it gets large:
  `docker compose exec postgres psql -U rateforge -c "TRUNCATE usage_events"`.
- With several workers, make sure database connections fit: connections = (`DB_POOL_SIZE` +
  `DB_MAX_OVERFLOW`) x workers x instances. `compose.load.yaml` raises PostgreSQL's limit to 300.
- Return to normal development mode with `docker compose down` and then
  `docker compose up -d --build`.

## What it showed (development sandbox)

These findings come from the reference runs in `performance.md`, on a deliberately small machine
(1 CPU shared by everything), so treat the *proportions* as informative and the absolute numbers
as not representative of real hardware.

**Where the gateway spends its CPU.** A sampling profiler (py-spy) on the gateway under the
`allowed` scenario attributed roughly: 22% to the call to the upstream, 19% to authentication
(key lookup), 12% to route and plan lookup, 12% to other database work, 10% to the Redis rate-limit
call, 11% to middleware (logging, metrics, usage), 5% to batched usage writes, and only about 1% to
the web framework itself. About half the CPU was database traffic, much of it re-reading routes that
almost never change.

**The fix, and its measured effect.** Caching routes in the process (5 second TTL) removed that
lookup from most requests. A/B runs alternating the cache off and on, otherwise identical:

| Route cache | Run 1 req/s | Run 2 req/s | Mean | p50 latency |
|---|---:|---:|---:|---:|
| off (`ROUTE_CACHE_TTL_SECONDS=0`) | 102.5 | 108.3 | 105.4 | 80-86 ms |
| on (default, 5 s) | 118.9 | 119.1 | 119.0 | 74 ms |

About **13% more throughput** with the cache on; the two runs of each setting did not overlap. It
is a small sample on a shared CPU, so read it as "clearly helps", not as a precise figure. The
trade-off is documented in [architecture](architecture.md): a route change made on a *different*
instance can take up to the TTL to be noticed.

**What was deliberately not cached.** API key lookups. Caching them would be the next big saving,
but it would delay key revocation by the TTL. That is a security trade-off worth deciding on
purpose, not slipping in for speed.

## Reading your own results

- If `baseline` is already close to `allowed`, the upstream (or the load generator) is your
  bottleneck, not the gateway. Add workers to the *upstream* or move the load generator off the
  machine.
- If `limited` is much faster than `allowed`, the cost is mostly in forwarding; if it is about the
  same, authentication and Redis dominate.
- Rising p99 with flat requests per second means requests are queueing; compare 1 vs 2 workers.
- Any non-zero **Failures** deserves investigation before you quote a number.
- 429s in `limited` and `mixed` are expected and are not failures.
