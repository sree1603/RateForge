# RateForge

Multi-tenant API gateway and rate-limiting platform (FastAPI, PostgreSQL, Redis).

## Status

- [x] Phase 1: project skeleton, Docker Compose, health endpoints, logging, Alembic setup
- [x] Phase 2: tenants, plans, API keys
- [x] Phase 3: reverse proxy
- [x] Phase 4: Redis token-bucket rate limiting
- [x] Phase 5: usage analytics
- [ ] Phase 6: CI, load tests, documentation

## Run locally

```bash
cp .env.example .env
docker compose up --build
```

Then check:

```bash
curl localhost:8000/health/live
curl localhost:8000/health/ready
```

Interactive docs: http://localhost:8000/docs

## Try the admin API

The admin token comes from `ADMIN_TOKEN` in `.env`. Interactive docs at `/docs` have an Authorize button for both headers.

```bash
ADMIN="X-Admin-Token: dev-only-admin-token-change-me"

# 1. Create the example plans (Basic 60/min, Pro 300/min, Enterprise 1000/min)
docker compose run --rm api python scripts/seed.py

# 2. Create a tenant
curl -s -X POST localhost:8000/api/v1/tenants -H "$ADMIN" -H "Content-Type: application/json" \
  -d '{"name": "acme"}'

# 3. Assign a plan (use the ids returned by GET /api/v1/plans and the tenant POST above)
curl -s localhost:8000/api/v1/plans -H "$ADMIN"
curl -s -X PUT localhost:8000/api/v1/tenants/<TENANT_ID>/plan -H "$ADMIN" \
  -H "Content-Type: application/json" -d '{"plan_id": "<PLAN_ID>"}'

# 4. Issue an API key. The "api_key" field is shown ONCE; copy it now.
curl -s -X POST localhost:8000/api/v1/keys -H "$ADMIN" -H "Content-Type: application/json" \
  -d '{"tenant_id": "<TENANT_ID>", "name": "my-first-key"}'

# 5. Authenticate with it
curl -s localhost:8000/api/v1/keys -H "X-API-Key: <API_KEY>"
```

## Try the gateway (Phase 3)

`scripts/seed.py` also registers a demo upstream (the `echo` container) and a route at
`/gateway/echo`. A tenant needs a plan before the gateway will serve it (Phase 4), so the quickest
way to get a working key is:

```bash
docker compose run --rm api python scripts/seed.py --demo-key
```

It creates a tenant `demo` on the Basic plan and prints an API key (shown once). Then:

```bash
curl "localhost:8000/gateway/echo/hello?x=1" -H "X-API-Key: <API_KEY>"
```

The echo service replies with what it received, so you can check that the path and query arrived,
that your `X-API-Key` was **not** forwarded, and that `X-Request-ID` and `X-Rateforge-Tenant` were
added. Other things to try: `/gateway/echo/slow?seconds=40` (504 after the upstream's read timeout),
`/gateway/echo/status/503` (passes through), `/gateway/echo/stream?chunks=5` (streams),
`/gateway/echo/%2e%2e/admin` (400).

Admin endpoints for the gateway: `POST/GET/PATCH /api/v1/upstreams` and `POST/GET/PATCH /api/v1/routes`.
Design decisions are written up in `docs/decisions/0003-gateway-proxy-design.md`.

## Rate limits and quotas (Phase 4)

Each tenant's plan sets a token bucket: `requests_per_window` tokens, refilled evenly over
`window_seconds`, optionally with a `monthly_quota`. Every forwarded request spends a token.
Responses carry `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`, and, when the
limit is hit, HTTP 429 with `Retry-After`.

Compose starts two gateway instances (`api` on port 8000, `api2` on port 8001) that share one
Redis. This script alternates requests between them and shows the limit is shared:

```bash
docker compose run --rm api python scripts/demo_rate_limit.py --key <API_KEY> --requests 100
```

With the Basic plan (60 per minute) the total allowed is about 60, not 120. Design notes and
trade-offs: `docs/decisions/0004-rate-limiting.md`. Settings (all optional):
`RATE_LIMIT_FAILURE_MODE` (`closed` or `open`), `REDIS_TIMEOUT_SECONDS`, `REDIS_MAX_CONNECTIONS`,
`POLICY_CACHE_TTL_SECONDS`.

## Usage analytics (Phase 5)

Every request from a tenant to a real route is recorded (tenant, route, method, status, outcome,
latency, request ID). Never stored: headers, query strings, bodies, keys, or paths beyond the
route prefix. Events are buffered in memory and written in batches, so they appear in the API
about a second after the request.

```bash
# Counts, error rate, p50/p95/p99 latency, top routes, timeline (24h by default).
curl "localhost:8000/api/v1/usage/summary" -H "X-API-Key: <API_KEY>"

# Individual requests, newest first. Filters: start, end, route, status_code, outcome.
curl "localhost:8000/api/v1/usage?outcome=rate_limited&limit=20" -H "X-API-Key: <API_KEY>"
```

Tenants see their own data; the admin token (`X-Admin-Token`) sees everyone's and can add
`tenant_id=<id>`. Both endpoints are also in the interactive docs at `/docs`.
Definitions, limits and trade-offs: `docs/decisions/0005-usage-tracking.md`. Optional settings:
`USAGE_QUEUE_SIZE`, `USAGE_BATCH_SIZE`, `USAGE_FLUSH_INTERVAL_SECONDS`.

## Run tests and lint

```bash
docker compose run --rm api pytest
docker compose run --rm api ruff check .
```

Integration tests need PostgreSQL and Redis (they create a `<database>_test` database and use Redis database 15) and are skipped if either is unreachable.

Or without Docker: `python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]" && pytest`

## Migrations

```bash
docker compose run --rm api alembic revision --autogenerate -m "message"
docker compose run --rm api alembic upgrade head
```
