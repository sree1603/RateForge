# Security

This is a threat model for the gateway: what could go wrong, what stops it, and which test proves
it. A control without a test is a claim; the tests are the evidence.

## Trust boundaries

- **Clients** (tenants) are untrusted. They hold an API key and may send anything.
- **Administrators** are trusted but not infallible, so the admin API validates what it is given.
- **Upstreams** are trusted to be the registered services, but their responses are still handled
  defensively (redirects not followed, hop-by-hop headers removed).
- **PostgreSQL and Redis** are trusted infrastructure on a private network.

## Threats and controls

| Threat | Control | Tests |
|---|---|---|
| Database leak exposes working API keys | Only an HMAC-SHA256 of each key is stored, keyed with a server secret; the key is shown once. A 256-bit random secret makes guessing infeasible. | `test_keys.py`, `test_security.py` |
| Key guessing / timing attacks | Constant-time comparison; every failure (unknown, wrong, revoked, expired, malformed) returns the same 401; a dummy comparison runs for unknown prefixes. | `test_authentication.py`, `test_credentials_and_logs.py` |
| Stolen key keeps working | Revocation is immediate (keys are never cached); expiry is enforced; suspended tenants are refused. | `test_keys.py` |
| **SSRF**: client makes the gateway call an internal address | Clients never supply a destination, only a route prefix. Admin-registered URLs are validated: http/https only; no credentials; link-local, unspecified, multicast and reserved addresses blocked, including decimal/hex/octal spellings and IPv4-mapped IPv6; metadata hostnames blocked. | `test_ssrf.py`, `test_forwarding_security.py` |
| **SSRF**: upstream redirects the gateway somewhere internal | Redirects are never followed; the 3xx goes back to the client. | `test_gateway.py`, `test_forwarding_security.py` |
| Path traversal / URL confusion (`..`, `%2e%2e`, `//host`, `@host`, backslashes) | Decoded *and* raw paths are checked; each segment is percent-encoded individually when the upstream URL is built, so nothing can change the host, scheme or query. | `test_routing_paths.py`, `test_forwarding_security.py` |
| Header injection / request smuggling via headers | Hop-by-hop headers (and any named in `Connection`) are dropped; gateway-owned headers (`X-Forwarded-For`, `X-RateForge-Tenant`, `X-Request-ID`) cannot be spoofed; inbound request IDs must match `[A-Za-z0-9._-]{1,64}`; encoded CR/LF in paths and queries stay inert text. | `test_proxy_headers.py`, `test_forwarding_security.py`, `test_credentials_and_logs.py` |
| Credential leakage to upstreams | `X-API-Key` and `X-Admin-Token` are stripped before forwarding. | `test_proxy_headers.py`, `test_gateway.py` |
| Credential leakage into logs, metrics, analytics or errors | Keys, tokens, query strings, headers and bodies are never logged or stored; metrics use only bounded labels; error bodies are generic. A planted leak makes the test fail (verified). | `test_credentials_and_logs.py`, `test_usage_recording.py`, `test_metrics.py` |
| SQL injection | All queries are parameterised through SQLAlchemy; hostile strings are stored and matched as plain data. | `test_input_hardening.py` |
| Malformed input causing 500s (NUL bytes, wrong types, huge values) | Names reject control characters; types, lengths and ranges are validated; route prefixes are checked before touching the database. | `test_input_hardening.py` |
| Oversized request bodies | Rejected from `Content-Length`, and cut off mid-stream for chunked bodies. | `test_gateway.py` |
| Tenant reads another tenant's data | Tenant scoping is applied server-side; client-supplied `tenant_id` filters are ignored for tenants; other tenants' key IDs return 404, not 403. | `test_keys.py`, `test_usage_api.py` |
| A tenant key used on admin endpoints | Separate credentials; admin endpoints require the admin token; the admin token is not accepted on the gateway. | `test_admin_and_plans.py`, `test_gateway.py` |
| Metrics reveal traffic patterns | `/metrics` requires the admin token (or `Authorization: Bearer`) unless deliberately made public. | `test_metrics.py` |
| Label-cardinality attack on metrics or analytics | Only real, registered route prefixes become labels or rows; unknown prefixes never do. | `test_metrics.py`, `test_usage_recording.py` |
| Dependency outages leaking internals | Database failures return a generic 503; real bugs remain 500s without detail. | `test_dependency_outages.py` |
| Abuse by a legitimate but over-eager client | Per-tenant token bucket and monthly quota, enforced atomically across instances. | `test_rate_limiter.py`, `test_gateway_limits.py` |

## Secrets

- `API_KEY_HMAC_SECRET` and `ADMIN_TOKEN` are required, have no defaults, and the app will not start
  without them. Values starting with `dev-only` are refused when `ENVIRONMENT=production`.
- Changing the HMAC secret invalidates every existing key.
- `.env` is git-ignored; only `.env.example` (placeholders) is committed.

## Known limits (deliberate, and worth knowing)

- **One shared admin token** instead of admin accounts and roles. Fine for a single operator;
  anything bigger needs real admin identities and audit trails.
- **DNS rebinding** is not fully closed. Upstream URLs are checked as written; a hostname could
  later resolve to an address that would have been refused. Production should also restrict
  outbound traffic at the network level and/or check the resolved address at connect time.
- **No TLS termination.** Run it behind a TLS-terminating proxy or load balancer.
- **No CORS handling.** This is a server-to-server gateway.
- **Rate limits are per tenant**, not per key or per route, so one noisy key can use up a tenant's
  allowance.
- **Private upstream addresses are allowed by default** (internal services are the normal case).
  Set `ALLOW_PRIVATE_UPSTREAMS=false` if every upstream is a public API.
