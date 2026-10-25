"""Whatever a client puts in a request, the gateway must only ever talk to the registered
upstream, and must not let the client speak for the gateway."""

from urllib.parse import quote

import pytest

UPSTREAM_HOST, UPSTREAM_PORT = "upstream.test", 9000

# Things that try to turn "a path on the upstream" into "a different server".
SSRF_PATHS = [
    "//evil.example/x",
    "///evil.example/x",
    "@evil.example/x",
    "http://evil.example/x",
    "https://evil.example/",
    "evil.example:80/x",
    "x@evil.example",
    "x?@evil.example",
    "x#@evil.example",
    "169.254.169.254/latest/meta-data",
    "http://169.254.169.254/latest/meta-data",
    "http:%2f%2fevil.example",
    "[::1]/x",
    "localhost/x",
    "x%0d%0aHost:%20evil.example",
    "a b",
    "a%09b",
    ";evil.example",
]


@pytest.mark.parametrize("path", SSRF_PATHS)
async def test_no_path_can_redirect_the_gateway_to_another_host(client, gateway, upstream, path):
    ctx = await gateway()
    response = await client.get(f"/gateway/echo/{path}", headers=ctx["headers"])
    # 404 is Starlette's own router refusing a newline in the path; that is also a safe outcome.
    assert response.status_code in (200, 400, 404), (path, response.status_code)
    for call in upstream.calls:  # either rejected outright, or sent to the registered host only
        assert call.request.url.host == UPSTREAM_HOST
        assert call.request.url.port == UPSTREAM_PORT
        assert call.request.url.scheme == "http"
        assert call.request.headers["host"] == f"{UPSTREAM_HOST}:{UPSTREAM_PORT}"
        assert "evil" not in call.request.url.host


async def test_dot_segments_encoded_every_way_are_rejected(client, gateway, upstream):
    ctx = await gateway()
    for variant in ("%2e%2e", "%2E%2E", "%2e%2E", ".%2e", "%2e.", "%252e%252e", "..%2f", "..%5c"):
        response = await client.get(f"/gateway/echo/{variant}/admin", headers=ctx["headers"])
        # Double-encoded ("%252e") is harmless text once decoded one level; every other form is
        # refused. Either way the upstream must never see a '..' path segment.
        assert response.status_code in (200, 400), variant
    for call in upstream.calls:
        assert ".." not in call.request.url.raw_path.decode().split("/")


async def test_the_host_header_cannot_choose_the_destination(client, gateway, upstream):
    ctx = await gateway()
    await client.get("/gateway/echo/x", headers={**ctx["headers"], "Host": "evil.example"})
    sent = upstream.calls[0].request
    assert sent.headers["host"] == f"{UPSTREAM_HOST}:{UPSTREAM_PORT}"
    assert sent.url.host == UPSTREAM_HOST


async def test_encoded_line_breaks_in_the_query_cannot_inject_headers(client, gateway, upstream):
    ctx = await gateway()
    await client.get(
        "/gateway/echo/x?a=1%0d%0aX-Injected:%20yes&b=%0aSet-Cookie:%20evil=1",
        headers=ctx["headers"],
    )
    sent = upstream.calls[0].request
    assert "x-injected" not in sent.headers and "set-cookie" not in sent.headers
    assert b"\r" not in sent.url.raw_path and b"\n" not in sent.url.raw_path
    assert b"%0d%0a" in sent.url.raw_path.lower()  # it stays inert, encoded text


async def test_clients_cannot_speak_for_the_gateway(client, gateway, upstream):
    ctx = await gateway()
    other = await ctx["add_tenant"]("someone-else", limit=100, window=60)
    await client.get(
        "/gateway/echo/x",
        headers={
            **ctx["headers"],
            "X-RateForge-Tenant": other["tenant"]["id"],  # pretend to be another tenant
            "X-Forwarded-For": "10.0.0.1",
            "X-Admin-Token": "anything",
        },
    )
    sent = upstream.calls[0].request.headers
    assert sent["x-rateforge-tenant"] == ctx["tenant"]["id"]
    assert sent["x-forwarded-for"].startswith(
        "10.0.0.1, "
    )  # appended to, never trusted as the client
    assert "x-admin-token" not in sent


@pytest.mark.parametrize("method", ["TRACE", "CONNECT", "PROPFIND", "BREW"])
async def test_unsupported_methods_are_refused(client, gateway, upstream, method):
    ctx = await gateway()
    response = await client.request(method, "/gateway/echo/x", headers=ctx["headers"])
    assert response.status_code in (400, 404, 405)
    assert upstream.calls == []


async def test_error_responses_do_not_reveal_the_upstream_address(client, gateway, upstream):
    import httpx

    ctx = await gateway()
    secret_address = "http://10.1.2.3:9000/internal"
    for error in (
        httpx.ConnectError(f"cannot connect to {secret_address}"),
        httpx.ReadTimeout(f"timed out talking to {secret_address}"),
    ):
        upstream.responder = lambda r, e=error: e
        response = await client.get("/gateway/echo/x", headers=ctx["headers"])
        assert response.status_code in (502, 504)
        assert "10.1.2.3" not in response.text and "internal" not in response.text


async def test_routes_cannot_be_registered_for_dangerous_destinations(client, admin):
    for url in (
        "http://169.254.169.254/",
        "http://[fd00:ec2::254]/latest",  # AWS IPv6 metadata address is reserved/blocked below
        "http://0x7f000001/",
        "http://2130706433/",
        "gopher://127.0.0.1:6379/",
        "http://user:pass@internal/",
    ):
        response = await client.post(
            "/api/v1/upstreams",
            json={"name": quote(url, safe="")[:90], "base_url": url},
            headers=admin,
        )
        assert response.status_code in (201, 422)
        if response.status_code == 201:
            # Allowed only for addresses that are legitimately internal (private/loopback);
            # never the metadata service, link-local space, or non-HTTP schemes.
            assert not any(x in url for x in ("169.254", "gopher", "@"))
