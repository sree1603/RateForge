# 0005: Usage tracking and analytics

Status: accepted (Phase 5)

## What is recorded, and what is not

One row in `usage_events` for each request from an **authenticated tenant to a real, enabled
route**: tenant, route (id and prefix), request ID, method, final status code, outcome, latency
and start time.

| Outcome | Meaning |
|---|---|
| `forwarded` | Passed the limiter and was sent to the upstream. The status may still be a 5xx. |
| `rate_limited` | Rejected by the per-window limit (429). |
| `quota_exceeded` | Rejected by the monthly quota (429). |
| `rejected` | Refused for another reason: 405, 400, 403 (no plan), 503 (limiter unavailable). |

**Not recorded:** requests with no valid key, requests to unknown or disabled routes, and
non-gateway requests. They have no tenant to attribute to, and recording attacker-chosen route
names would let anyone grow the table. They still appear in the structured logs.

**Never stored:** headers, query strings, request or response bodies, API keys, or the request
path beyond the route prefix (paths often contain IDs or personal data). This is tested.

## Where recording happens

The request middleware creates a small mutable context before the request runs. The gateway
handler fills it in as it learns things (tenant, route, outcome). When the response has finished,
the middleware turns it into a record. Consequences:

- **Latency and status are the real end-to-end values**, including time spent streaming the
  response body to the client, and including failures that happen after the handler returns.
- The gateway handler stays free of recording code beyond three assignments.

Latency is measured at the gateway (receive to last byte sent), so it includes the upstream's
time. It is not the client's view: network time between client and gateway is not included.

## The recorder interface and the buffered implementation

Code depends only on `record(event)`. The current implementation puts events on an in-process
queue; a background task writes them to PostgreSQL in batches (every 100 events or every second,
whichever comes first). `record()` never blocks and never raises. Shutdown flushes the queue
before the database connection is closed (tested live: events that had not yet been flushed were
stored after a graceful stop).

**Honest limits:**
- Events still queued are lost if the process is killed (crash, SIGKILL, power loss).
- A batch that fails to write is dropped and counted (`recorder.failed`); nothing retries it.
- If the queue fills (database much slower than traffic) new events are dropped and counted
  (`recorder.dropped`) rather than making requests wait.

So usage data here is **best-effort analytics, not an audit log or a billing ledger**. The
monthly quota used for enforcement lives in Redis and is separate. For stronger guarantees, swap
the recorder for one that writes to a durable queue (Redis Streams, Kafka, SQS); the gateway
code does not change.

## Query API

- `GET /api/v1/usage`: individual events, newest first, with filters (time range, route, status
  code, outcome) and **keyset pagination**. The cursor is a position `(time, id)`, not an offset,
  so pages do not shift or repeat when new events arrive, and rows with identical timestamps are
  neither skipped nor duplicated (both tested).
- `GET /api/v1/usage/summary`: totals, accepted vs rejected, status classes, error rate,
  p50/p95/p99 latency, top routes and a timeline, computed in SQL.
- Tenants see only their own events; the `tenant_id` filter is honoured for admins only.
- Default range is the last 24 hours; the maximum is 366 days. A timeline may have at most 1,000
  buckets (so one-minute buckets need a range under about 16 hours).

**Definitions** (also in the OpenAPI docs):
- *accepted* = forwarded; *rejected* = every other outcome.
- *error rate* = 5xx responses as a share of forwarded requests; null when nothing was forwarded.
- *latency percentiles* use forwarded requests only, so near-instant 429s do not make the
  service look faster than it is. They are exact (`percentile_cont`), not approximate.
- *status classes* count all recorded requests, including gateway-generated 429s.
- *timeline* lists only buckets that contain events, aligned to UTC.

## Storage and scale

`usage_events` has two indexes: `(tenant_id, occurred_at)` for per-tenant queries and
`(occurred_at)` for all-tenant queries. This is adequate for millions of rows. Beyond that, the
usual next steps are time-based partitioning, pre-aggregated hourly rollups (so summaries do not
scan raw rows), and a retention job. **No retention policy is implemented**; rows accumulate until
you delete them.

## Not done (by design)

- No per-API-key breakdown (events are per tenant).
- No export endpoint or streaming of raw events.
- Percentiles are recomputed from raw rows on each request; there is no caching.
