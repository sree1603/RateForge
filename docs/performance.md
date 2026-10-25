# Performance results

Everything below was **measured**, not estimated. Each row comes from a run of
`scripts/run_load_suite.py`; the raw Locust CSV files are next to this document in
`performance-results/<label>/`.

How to read these numbers:

- Users send requests back to back with no think time, so this is closer to a *maximum* than to
  typical traffic.
- The load generator, gateway, PostgreSQL, Redis and the upstream all share one machine in these
  runs, so they compete for the same CPU. A dedicated load-generator machine would show higher
  numbers. Treat the figures as a baseline for *this* setup, and compare configurations only
  against each other.
- Latency is end to end as seen by the load generator, in milliseconds.
- "baseline" calls the upstream directly. The gap between it and "allowed" is the gateway's
  overhead (authentication, route lookup, rate limiting, proxying, usage recording).
- Runs whose machine is described as a **development sandbox** (1 CPU, shared by everything) are
  reference runs that validate the harness and the A/B comparisons. They are *not* what this
  software can do on real hardware. Add your own runs and compare configurations against each other.
- In "limited" and "mixed", HTTP 429 is an expected answer, not a failure. "Failures" counts only
  unexpected responses (5xx, timeouts, and so on).


## ab-cache-off-1

- machine: development sandbox: 1 vCPU, 4 GB RAM, shared by everything
- visible to the test container: 1 CPUs, 3.9 GB RAM
- 10 concurrent users, 12 s per scenario
- 1 gateway instance(s)
- date: 2026-10-24 14:24 UTC
- notes: A/B test: ROUTE_CACHE_TTL_SECONDS=0, otherwise identical; 1 worker, usage recording on

| Scenario | Requests/s | p50 ms | p95 ms | p99 ms | Responses | Failures |
|---|---:|---:|---:|---:|---|---:|
| allowed | 102 | 86.0 | 170 | 270 | 200: 100% | 0 |

## ab-cache-off-2

- machine: development sandbox: 1 vCPU, 4 GB RAM, shared by everything
- visible to the test container: 1 CPUs, 3.9 GB RAM
- 10 concurrent users, 12 s per scenario
- 1 gateway instance(s)
- date: 2026-10-24 14:24 UTC
- notes: A/B test: ROUTE_CACHE_TTL_SECONDS=0, otherwise identical; 1 worker, usage recording on

| Scenario | Requests/s | p50 ms | p95 ms | p99 ms | Responses | Failures |
|---|---:|---:|---:|---:|---|---:|
| allowed | 108 | 80.0 | 170 | 220 | 200: 100% | 0 |

## ab-cache-on-1

- machine: development sandbox: 1 vCPU, 4 GB RAM, shared by everything
- visible to the test container: 1 CPUs, 3.9 GB RAM
- 10 concurrent users, 12 s per scenario
- 1 gateway instance(s)
- date: 2026-10-24 14:24 UTC
- notes: A/B test: ROUTE_CACHE_TTL_SECONDS=5, otherwise identical; 1 worker, usage recording on

| Scenario | Requests/s | p50 ms | p95 ms | p99 ms | Responses | Failures |
|---|---:|---:|---:|---:|---|---:|
| allowed | 119 | 74.0 | 150 | 210 | 200: 100% | 0 |

## ab-cache-on-2

- machine: development sandbox: 1 vCPU, 4 GB RAM, shared by everything
- visible to the test container: 1 CPUs, 3.9 GB RAM
- 10 concurrent users, 12 s per scenario
- 1 gateway instance(s)
- date: 2026-10-24 14:24 UTC
- notes: A/B test: ROUTE_CACHE_TTL_SECONDS=5, otherwise identical; 1 worker, usage recording on

| Scenario | Requests/s | p50 ms | p95 ms | p99 ms | Responses | Failures |
|---|---:|---:|---:|---:|---|---:|
| allowed | 119 | 74.0 | 150 | 210 | 200: 100% | 0 |

## sandbox smoke test

- machine: development sandbox: 1 vCPU, 4 GB RAM, shared by the gateway, PostgreSQL, Redis, upstream and load generator
- visible to the test container: 1 CPUs, 3.9 GB RAM
- 10 concurrent users, 8 s per scenario
- 1 gateway instance(s)
- date: 2026-10-24 08:50 UTC
- notes: harness validation only; 1 uvicorn worker, usage recording on

| Scenario | Requests/s | p50 ms | p95 ms | p99 ms | Responses | Failures |
|---|---:|---:|---:|---:|---|---:|
| baseline | 1,156 | 5.0 | 9.0 | 12.0 | 200: 100% | 0 |
| allowed | 137 | 63.0 | 130 | 180 | 200: 100% | 0 |
| limited | 233 | 38.0 | 67.0 | 110 | 200: 1%, 429: 99% | 0 |
| mixed | 146 | 62.0 | 120 | 160 | 200: 93%, 429: 7% | 0 |
