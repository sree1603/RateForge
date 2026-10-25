# 0006: Hardening and operations

Status: accepted (Phase 6)

## Metrics

Prometheus text format at `/metrics`, protected by the admin token (also accepted as
`Authorization: Bearer`, which is what a scrape config can send). Set `METRICS_PUBLIC=true` only if
the endpoint is unreachable from outside.

Label values come from small fixed sets: HTTP method (others become `OTHER`), status class, a
*registered* route prefix, and the four outcomes. Never a path, tenant ID, key, or client-chosen
string. Every distinct label value is a time series that lives until restart, so letting clients
choose labels would let them exhaust memory. A test sends hostile paths and prefixes and checks
none appear.

With multiple worker processes in one container, set `PROMETHEUS_MULTIPROC_DIR` so their numbers are
combined; otherwise a scrape sees only the worker that answered. Scaling by adding containers needs
nothing special.

## Dependency outages return 503, not 500

Database connection failures (refused, timed out, pool exhausted, dropped mid-query) are mapped to
`503` with `Retry-After: 1` and a generic body. Bugs are still 500s. Without this a database outage
looked like a defect in the gateway and leaked a stack trace into the log for every request.

Connection and pool timeouts are explicit (`DB_CONNECT_TIMEOUT_SECONDS`, `DB_POOL_TIMEOUT_SECONDS`)
so an outage fails in seconds, not after the driver's default of a minute.

## Route cache

See [architecture](../architecture.md#caches-and-how-stale-they-can-be). Added after profiling
showed about half the gateway's CPU going to database work, much of it re-reading routes. Measured
effect and caveats are in [load testing](../load-testing.md). API keys are intentionally not cached
so revocation stays immediate.

## Input hardening found by testing

Writing hostile-input tests found two real problems, both fixed:

- A NUL byte in a name or in a `/gateway/<prefix>` URL reached PostgreSQL, which rejects it, and the
  request became a 500. Names now reject control characters, the usage `route` filter does too, and
  gateway prefixes are checked against the allowed pattern before any query.
- Inbound `X-Request-ID` values were echoed into response headers, logs and upstream requests with
  only a length check. They must now match `[A-Za-z0-9._-]{1,64}` or a fresh ID is generated.

## CI

GitHub Actions runs on every push and pull request:

- **test job** (Postgres and Redis service containers): lint, formatting, migrations on an empty
  database, a drift check (`alembic check` fails if models and migrations disagree), a downgrade and
  re-upgrade, then the full test suite with a coverage gate of 90%.
- **docker job**: builds the images, starts the stack, seeds it, and runs `scripts/smoke_test.py`
  through the real HTTP interface.

`REQUIRE_SERVICES=1` makes the integration tests *fail* instead of skip when a service container is
unreachable, so a broken container cannot turn the suite into a green no-op.

## Tests found real bugs in my own changes

Worth recording because it is the reason the test suite is shaped as it is: a refactor accidentally
removed creation of the gateway's HTTP client from startup. Every request-level test injects its own
client, so only a test that runs the real startup (`test_lifespan.py`) caught it. The smoke test and
the lifespan test exist for that class of mistake.

## Not done

- No circuit breaker; a hung dependency costs its timeout on each request.
- No Grafana dashboards or alert rules; the metrics are exposed, not yet used.
- No distributed tracing.
- Load results come from a single machine with the load generator on the same host.
