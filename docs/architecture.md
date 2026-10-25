# Architecture

RateForge is one FastAPI application backed by PostgreSQL (durable data) and Redis (shared,
short-lived rate-limit state). It has two logical halves, deployed together:

- **Data plane**: the `/gateway/...` path. Authenticates the caller, applies the tenant's plan,
  forwards the request, records what happened. Kept as short as possible.
- **Control plane**: the `/api/v1/...` admin and reporting API. Creates tenants, plans, keys,
  upstreams and routes, and answers usage questions. Its changes decide how the data plane behaves.

They are separate routers and service modules in one process. That keeps the project simple to run
while leaving a clear line along which they could be split into separate deployments later.

```mermaid
flowchart LR
    Client[API client] -->|X-API-Key| GW
    Admin[Administrator] -->|X-Admin-Token| GW
    subgraph App["RateForge (FastAPI)"]
        GW["Gateway /gateway/..."]
        API["Admin and reporting API /api/v1/..."]
    end
    GW --> PG[("PostgreSQL<br/>tenants, plans, keys,<br/>routes, usage events")]
    API --> PG
    GW --> RD[("Redis<br/>token buckets,<br/>monthly quota counters")]
    GW -->|"forward (timeouts, no retries)"| UP[Registered upstream APIs]
    GW -.->|"batched, after the response"| PG
```

## What happens to a request

```mermaid
sequenceDiagram
    participant C as Client
    participant G as Gateway
    participant P as PostgreSQL
    participant R as Redis
    participant U as Upstream
    C->>G: GET /gateway/weather/current  (X-API-Key)
    G->>P: find key by prefix, verify HMAC (the only per-request DB read)
    Note over G: route and plan come from the in-process caches,<br/>or from PostgreSQL if the cache is cold
    G->>R: spend a token and a quota unit (one atomic Lua call)
    alt limit or quota exceeded
        G-->>C: 429 + Retry-After + X-RateLimit-*
    else allowed
        G->>U: forward request (connect/read timeouts)
        U-->>G: response (streamed)
        G-->>C: same status and body + X-RateLimit-*
    end
    G--)P: usage event (queued, written in batches)
```

In words:

1. **Receive.** Middleware assigns a request ID (or accepts a safe inbound one) and starts timing.
2. **Authenticate.** The key `rf_<prefix>_<secret>` is split; the row is found by prefix and the
   whole key is verified against a stored HMAC-SHA256 with a constant-time comparison. Unknown,
   malformed, wrong, revoked and expired keys all produce the same 401.
3. **Resolve the route.** `/gateway/<prefix>/...` must match a registered, enabled route whose
   upstream is also enabled; the HTTP method must be allowed; the path must be free of traversal
   tricks. The destination is **always** the admin-registered upstream. A client cannot supply one.
4. **Resolve the plan and rate limit.** The tenant's plan sets a token bucket and optional monthly
   quota. One Lua script in Redis checks and updates both atomically, so any number of gateway
   instances enforce one shared limit.
5. **Forward.** The request goes to the upstream with hop-by-hop headers removed, the client's
   credentials stripped, and gateway headers added. Bodies stream in both directions. There are no
   automatic retries.
6. **Respond.** The upstream's status, headers and body are streamed back with rate-limit headers.
7. **Record.** After the last byte, the middleware logs one structured line, updates metrics, and
   queues a usage event.

## Data model

```mermaid
erDiagram
    TENANT ||--o{ API_KEY : has
    TENANT ||--o{ TENANT_PLAN : "plan history"
    PLAN ||--o{ TENANT_PLAN : "applied via"
    UPSTREAM ||--o{ ROUTE : serves
    TENANT ||--o{ USAGE_EVENT : generates
    ROUTE ||--o{ USAGE_EVENT : "recorded for"
    TENANT {
        uuid id
        string name
        string status
    }
    API_KEY {
        uuid id
        string key_prefix
        string key_hash
        string status
        datetime expires_at
    }
    PLAN {
        uuid id
        string name
        int requests_per_window
        int window_seconds
        int monthly_quota
    }
    TENANT_PLAN {
        uuid tenant_id
        uuid plan_id
        datetime effective_from
        datetime effective_until
    }
    UPSTREAM {
        uuid id
        string base_url
        float connect_timeout
        float read_timeout
        bool enabled
    }
    ROUTE {
        uuid id
        string path_prefix
        array allowed_methods
        bool strip_prefix
        bool enabled
    }
    USAGE_EVENT {
        bigint id
        uuid tenant_id
        string route_prefix
        int status_code
        string outcome
        float latency_ms
        datetime occurred_at
    }
```

Plan changes close the current `tenant_plans` row and open a new one, so history is kept.

## Where each piece of state lives

| State | Where | Why |
|---|---|---|
| Tenants, plans, keys (as hashes), routes, upstreams | PostgreSQL | Durable business data |
| Usage events | PostgreSQL (written in batches) | Queryable history; best-effort, see below |
| Token buckets, monthly quota counters | Redis | Updated on every request; must be shared by all instances; fine to lose |
| Resolved plan per tenant (30 s), route snapshot (5 s) | In-process cache | Avoids a database read per request |
| Usage events not yet written | In-process queue | Keeps recording off the request path |
| Metrics | In-process (or per-worker files) | Scraped by Prometheus |

## Caches and how stale they can be

Speed comes from caching things that rarely change. The price is a delay before changes made
*somewhere else* are noticed. Changes made through the same process take effect immediately.

| Cache | Default | After a change made on another instance | Setting |
|---|---|---|---|
| Tenant plan | 30 s | Up to 30 s | `POLICY_CACHE_TTL_SECONDS` |
| Route and upstream settings | 5 s | Up to 5 s (a disabled route can keep serving this long) | `ROUTE_CACHE_TTL_SECONDS` (0 turns it off) |
| API key lookup | none | Revocation is immediate | n/a |

API keys are deliberately **not** cached, so revoking a key stops it at once. The cost is that the
gateway needs PostgreSQL for every request, so a database outage is a gateway outage (a clean 503;
see [failure scenarios](failure-scenarios.md)).

## Code layout

```
app/
  main.py               application, startup and shutdown
  core/                 configuration, security primitives, logging, middleware,
                        error handling, URL (SSRF) validation, usage context
  api/                  gateway, health, metrics, and v1/ admin and reporting routers
  services/             business rules: authentication, policy, routing, proxy,
                        rate limiter, usage recorder
  repositories/         database queries
  models/ schemas/      database tables and request/response shapes
  observability/        Prometheus metrics
  db/                   session and Alembic migrations
tests/                  unit, integration, security, load
scripts/                seed, demo, load-test runner, smoke test, echo upstream
docs/                   this documentation and design decisions
```

Routers handle HTTP, services hold rules, repositories hold queries. A router that contains a
query, or a service that knows about HTTP, is a bug.

## Scaling notes

- **More instances** behind a load balancer work as they are: limits are shared through Redis,
  data through PostgreSQL. Cache staleness grows only by the TTLs above.
- **More workers per instance** work too, but each worker has its own caches and usage queue, and
  metrics need `PROMETHEUS_MULTIPROC_DIR` set to be combined.
- **Connection budget**: PostgreSQL connections = pool size + overflow, times workers, times
  instances. Keep that below PostgreSQL's `max_connections`.
- **Bottlenecks to expect first**: the per-request key lookup in PostgreSQL, then Redis round
  trips. See [performance](performance.md) and [load testing](load-testing.md) for measurements.
