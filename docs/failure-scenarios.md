# Failure scenarios

What a client sees when something breaks, and how the gateway recovers. Every row below was
**observed by deliberately breaking the dependency** and calling the real running gateway (not
assumed from the code), and each behaviour is also covered by automated tests.

## Summary

| What breaks | Gateway request | Admin API | `/health/live` | `/health/ready` | Recovery |
|---|---|---|---|---|---|
| Upstream is slow (over its read timeout) | **504**, after exactly the timeout | works | 200 | 200 | none needed |
| Upstream is not running | **502** (fast) | works | 200 | 200 | automatic |
| Upstream returns 5xx or 4xx | passed through unchanged | works | 200 | 200 | n/a |
| Upstream redirects (e.g. to a metadata address) | the **3xx is returned**, never followed | works | 200 | 200 | n/a |
| Redis down, `closed` (default) | **503** + `Retry-After: 1` | works | 200 | **503** (degraded) | automatic |
| Redis down, `open` | **200**, forwarded without limiting | works | 200 | 503 (degraded) | automatic |
| PostgreSQL down | **503** + `Retry-After: 1` | **503** + `Retry-After: 1` | 200 | **503** (degraded) | automatic, no failed request after it returns |
| Database pool exhausted | **503** | **503** | 200 | n/a | automatic |
| Request body too large | **413** | n/a | 200 | 200 | n/a |

No automatic retries are ever made for proxied requests: retrying a POST could repeat its effect.

## Observed output

Run against a live gateway (read timeout 1 s). `retry-after` is blank when the header is absent.

```
### UPSTREAM PROBLEMS
upstream slow (sleeps 3s)                      HTTP 504 in  1.01s   {"detail":"Upstream timed out"}
upstream returns 500                           HTTP 500 in  0.01s
upstream returns 404                           HTTP 404 in  0.01s
upstream not running (connection refused)      HTTP 502 in  0.01s   {"detail":"Upstream is unreachable or returned an invalid response"}
upstream redirects to metadata address         HTTP 302 in  0.01s

### REDIS DOWN (default: fail closed)
gateway request                                HTTP 503 in  0.01s  retry-after=1   {"detail":"Rate limiter unavailable; try again shortly"}
liveness                                       HTTP 200 in  0.00s                  {"status":"ok"}
readiness                                      HTTP 503 in  0.00s                  {"status":"degraded","checks":{"postgres":"ok","redis":"unavailable"}}
admin API (does not need Redis)                HTTP 200 in  0.01s
  /metrics: rateforge_limiter_failures_total 1.0
gateway request after Redis returns            HTTP 200 in  0.01s

### REDIS DOWN, RATE_LIMIT_FAILURE_MODE=open
gateway request (fails open)                   HTTP 200 in  0.09s

### POSTGRESQL DOWN
gateway request                                HTTP 503 in  0.03s  retry-after=1   {"detail":"Service temporarily unavailable; try again shortly"}
admin API                                      HTTP 503 in  0.00s  retry-after=1   {"detail":"Service temporarily unavailable; try again shortly"}
liveness                                       HTTP 200 in  0.00s                  {"status":"ok"}
readiness                                      HTTP 503 in  0.00s                  {"status":"degraded","checks":{"postgres":"unavailable","redis":"ok"}}
  /metrics: rateforge_dependency_errors_total{dependency="database"} 2.0

### POSTGRESQL BACK
gateway request (first after restart)          HTTP 200 in  0.06s
gateway request (second)                       HTTP 200 in  0.01s
readiness                                      HTTP 200 in  0.00s                  {"status":"ready","checks":{"postgres":"ok","redis":"ok"}}
```

These timings are for a refused connection (a stopped service). A service that *hangs* instead
(network black hole) costs the configured timeout on each request: 0.5 s for Redis
(`REDIS_TIMEOUT_SECONDS`), 5 s to connect and 5 s waiting for a free database connection
(`DB_CONNECT_TIMEOUT_SECONDS`, `DB_POOL_TIMEOUT_SECONDS`). A circuit breaker that stops trying for
a few seconds after a failure would remove that cost; it is a sensible next step and is not built.

## Choosing fail-open or fail-closed

`RATE_LIMIT_FAILURE_MODE` decides what happens when Redis is unreachable.

- **closed** (default): requests get a 503. Upstreams stay protected, quotas stay accurate, and the
  failure is loud. Clients see an outage while Redis is down.
- **open**: requests are forwarded with no limiting and no quota counting. Clients see no outage,
  but limits are unenforced for the duration and a misbehaving client could flood an upstream.

Pick closed when upstream protection or quota accuracy matters more than uptime; pick open when the
reverse is true. The default is closed because a limiter that silently stops limiting is a quiet
failure.

## What is lost during an outage

- **Redis down:** nothing durable. Token buckets and monthly quota counters live in Redis, and a
  restart without persistence resets them (clients get a fresh allowance).
- **PostgreSQL down:** usage events that could not be written are dropped and counted
  (`rateforge_usage_events_total{result="failed"}`). Events still queued when the process is
  *killed* are lost too; on a graceful stop they are flushed.

## Try it yourself (Docker, PowerShell)

```powershell
# Redis outage: expect 503 + Retry-After, readiness degraded, then recovery
docker compose stop redis
curl.exe -i "http://localhost:8000/gateway/echo/hello" -H "X-API-Key: <KEY>"
curl.exe "http://localhost:8000/health/ready"
docker compose start redis

# PostgreSQL outage: expect 503 on both the gateway and the admin API
docker compose stop postgres
curl.exe -i "http://localhost:8000/gateway/echo/hello" -H "X-API-Key: <KEY>"
docker compose start postgres

# Slow upstream: expect 504 after the upstream's read timeout (30 s by default)
curl.exe -i "http://localhost:8000/gateway/echo/slow?seconds=40" -H "X-API-Key: <KEY>"

# Upstream down
docker compose stop echo
curl.exe -i "http://localhost:8000/gateway/echo/hello" -H "X-API-Key: <KEY>"
docker compose start echo
```
