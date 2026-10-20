# 0003: Gateway proxy design

Status: accepted (Phase 3)

## Decisions

**Destinations are registered by admins, never supplied by clients.** A client chooses only a
route prefix (`/gateway/<prefix>/...`) from a fixed list. This is the main defence against SSRF.
On registration the URL is also validated: http/https only, no credentials, no query string,
and link-local, unspecified, multicast and reserved addresses are refused, including the
decimal, hex and octal spellings of an IPv4 address that resolvers still accept.

**Known limitation.** Validation looks at the URL as written. A hostname could resolve to a bad
address, or be repointed later (DNS rebinding). Real hardening is network-level egress
filtering plus checking the resolved address at connect time. Private and loopback addresses are
allowed by default because internal services are the normal use; set
`ALLOW_PRIVATE_UPSTREAMS=false` for deployments that only call public APIs.

**Paths are validated and rebuilt, not concatenated.** Dot segments, empty segments,
backslashes, control characters and percent-encoded dots/slashes/NUL (checked on the raw path)
are rejected with 400. Each remaining segment is percent-encoded individually when the upstream
URL is built, so a decoded `%2e%2e` reaches the upstream as literal text, not as `..`.

**Headers.** Hop-by-hop headers (and any header named in `Connection`) are dropped in both
directions. The client's `X-API-Key` and `X-Admin-Token` are never forwarded. The gateway sets
`X-Request-ID`, `X-Forwarded-For` (appended) and `X-RateForge-Tenant`, replacing any
client-supplied values. `Authorization` is forwarded, since clients use `X-API-Key` for the
gateway and `Authorization` is for the upstream.

**No automatic retries.** Retrying a non-idempotent request (POST, PUT, PATCH, DELETE) can repeat
its side effects. Failures are reported: 502 unreachable, 504 timeout, 503 connection pool
exhausted, 413 body too large. Upstream 4xx/5xx responses pass through unchanged.

**Redirects are not followed.** Otherwise an upstream could redirect the gateway to an address
we refused at registration. The 3xx is returned to the client as-is.

**Streaming.** Request bodies are streamed up with a size cap (`GATEWAY_MAX_BODY_BYTES`, default
10 MB) enforced both from `Content-Length` and while streaming chunked bodies. Responses are
streamed back without buffering. Response bytes are passed through untouched, so the gateway asks
for `Accept-Encoding: identity` unless the client sent its own preference.

**The database connection is released before the upstream call.** Authentication and route lookup
need the database; waiting on a slow upstream does not. Closing the session first means in-flight
proxied requests do not each hold a pooled connection.

**The middleware is pure ASGI** (not `@app.middleware("http")`) so streamed responses pass
through without extra wrapping.

## Not done yet (by design)

- Route lookups hit the database on every request. A short-lived cache arrives with the policy
  cache in Phase 4.
- Rate limiting (Phase 4) and usage recording (Phase 5) hook in after authentication and around
  the proxy call respectively.
- WebSocket and other upgrade requests are not proxied.
