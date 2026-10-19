# RateForge

Multi-tenant API gateway and rate-limiting platform (FastAPI, PostgreSQL, Redis).

## Status

- [x] Phase 1: project skeleton, Docker Compose, health endpoints, logging, Alembic setup
- [x] Phase 2: tenants, plans, API keys
- [ ] Phase 3: reverse proxy
- [ ] Phase 4: Redis token-bucket rate limiting
- [ ] Phase 5: usage analytics
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

## Run tests and lint

```bash
docker compose run --rm api pytest
docker compose run --rm api ruff check .
```

Integration tests need PostgreSQL (they create a `<database>_test` database automatically) and are skipped if it is unreachable.

Or without Docker: `python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]" && pytest`

## Migrations

```bash
docker compose run --rm api alembic revision --autogenerate -m "message"
docker compose run --rm api alembic upgrade head
```
