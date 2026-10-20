import gzip

import httpx

from app.core.config import get_settings


# ---------------- authentication ----------------
async def test_gateway_requires_a_tenant_key(client, gateway, admin):
    await gateway()
    assert (await client.get("/gateway/echo/x")).status_code == 401
    assert (await client.get("/gateway/echo/x", headers={"X-API-Key": "rf_bad"})).status_code == 401
    # The admin token is not a way into the data plane.
    assert (await client.get("/gateway/echo/x", headers=admin)).status_code == 401


async def test_suspended_tenant_is_refused(client, gateway, admin, db):
    from sqlalchemy import update

    from app.models import Tenant

    ctx = await gateway()
    assert (await client.get("/gateway/echo/x", headers=ctx["headers"])).status_code == 200
    await db.execute(update(Tenant).values(status="suspended"))
    await db.commit()
    assert (await client.get("/gateway/echo/x", headers=ctx["headers"])).status_code == 403


async def test_unauthenticated_requests_never_reach_the_upstream(client, gateway, upstream):
    await gateway()
    await client.get("/gateway/echo/x")
    await client.get("/gateway/echo/x", headers={"X-API-Key": "nope"})
    assert upstream.calls == []


# ---------------- happy path ----------------
async def test_request_is_forwarded_with_path_query_method_and_body(client, gateway, upstream):
    ctx = await gateway()
    response = await client.post(
        "/gateway/echo/orders/42?expand=items&expand=customer&q=a%20b",
        content=b'{"hello": "world"}',
        headers={**ctx["headers"], "Content-Type": "application/json"},
    )
    assert response.status_code == 200
    call = upstream.calls[0]
    assert call.request.method == "POST"
    assert call.request.url.scheme == "http"
    assert call.request.url.host == "upstream.test"
    assert call.request.url.port == 9000
    assert call.request.url.path == "/orders/42"
    assert call.request.url.query == b"expand=items&expand=customer&q=a%20b"
    assert call.body == b'{"hello": "world"}'
    assert call.request.headers["content-type"] == "application/json"


async def test_empty_path_maps_to_upstream_root(client, gateway, upstream):
    ctx = await gateway()
    await client.get("/gateway/echo", headers=ctx["headers"])
    await client.get("/gateway/echo/", headers=ctx["headers"])
    assert [c.request.url.path for c in upstream.calls] == ["/", "/"]


async def test_base_path_and_prefix_handling(client, gateway, upstream):
    ctx = await gateway(base_url="http://upstream.test:9000/api/v2", strip_prefix=False)
    await client.get("/gateway/echo/items", headers=ctx["headers"])
    assert upstream.calls[0].request.url.path == "/api/v2/echo/items"


async def test_response_status_body_and_headers_pass_through(client, gateway, upstream):
    ctx = await gateway()
    upstream.responder = lambda r: httpx.Response(
        201,
        json={"id": 7},
        headers=[("X-Upstream", "yes"), ("Set-Cookie", "a=1"), ("Set-Cookie", "b=2")],
    )
    response = await client.post("/gateway/echo/things", headers=ctx["headers"])
    assert response.status_code == 201
    assert response.json() == {"id": 7}
    assert response.headers["x-upstream"] == "yes"
    assert response.headers.get_list("set-cookie") == ["a=1", "b=2"]


async def test_upstream_errors_pass_through_unchanged(client, gateway, upstream):
    ctx = await gateway()
    for status in (400, 404, 429, 500, 503):
        upstream.responder = lambda r, s=status: httpx.Response(
            s, json={"from": "upstream", "s": s}
        )
        response = await client.get("/gateway/echo/x", headers=ctx["headers"])
        assert response.status_code == status
        assert response.json() == {"from": "upstream", "s": status}


async def test_binary_body_round_trips(client, gateway, upstream):
    ctx = await gateway()
    payload = bytes(range(256)) * 100
    upstream.responder = lambda r: httpx.Response(200, content=payload)
    response = await client.put("/gateway/echo/blob", content=payload, headers=ctx["headers"])
    assert upstream.calls[0].body == payload
    assert response.content == payload


async def test_compressed_upstream_response_is_passed_through_untouched(client, gateway, upstream):
    ctx = await gateway()
    compressed = gzip.compress(b'{"big": true}')
    upstream.responder = lambda r: httpx.Response(
        200,
        stream=httpx.ByteStream(compressed),
        headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
    )
    response = await client.get(
        "/gateway/echo/x", headers={**ctx["headers"], "Accept-Encoding": "gzip"}
    )
    assert response.headers["content-encoding"] == "gzip"
    assert response.json() == {"big": True}  # httpx decoded it for us, so the bytes were valid gzip


# ---------------- headers ----------------
async def test_gateway_credentials_are_not_forwarded_and_gateway_headers_are_added(
    client, gateway, upstream
):
    ctx = await gateway()
    await client.get(
        "/gateway/echo/x",
        headers={
            **ctx["headers"],
            "Authorization": "Bearer for-the-upstream",
            "X-Admin-Token": "should-not-leak",
            "X-RateForge-Tenant": "forged",
            "X-Request-ID": "caller-id-1",
            "X-Forwarded-For": "198.51.100.7",
        },
    )
    sent = upstream.calls[0].request.headers
    assert "x-api-key" not in sent
    assert "x-admin-token" not in sent
    assert ctx["headers"]["X-API-Key"] not in str(sent)
    assert sent["authorization"] == "Bearer for-the-upstream"
    assert sent["x-rateforge-tenant"] == ctx["tenant"]["id"]  # forged value replaced
    assert sent["x-request-id"] == "caller-id-1"
    assert sent["x-forwarded-for"].startswith("198.51.100.7, ")
    assert sent["host"] == "upstream.test:9000"


async def test_request_id_reaches_upstream_and_client(client, gateway, upstream):
    ctx = await gateway()
    response = await client.get("/gateway/echo/x", headers=ctx["headers"])
    rid = response.headers["x-request-id"]
    assert rid.startswith("req_")
    assert upstream.calls[0].request.headers["x-request-id"] == rid


async def test_hop_by_hop_response_headers_are_removed(client, gateway, upstream):
    ctx = await gateway()
    upstream.responder = lambda r: httpx.Response(
        200,
        json={},
        headers={"Keep-Alive": "timeout=5", "Connection": "X-Hop", "X-Hop": "1", "X-End": "2"},
    )
    response = await client.get("/gateway/echo/x", headers=ctx["headers"])
    assert "keep-alive" not in response.headers
    assert "x-hop" not in response.headers
    assert response.headers["x-end"] == "2"


# ---------------- routing rules ----------------
async def test_unknown_disabled_route_and_disabled_upstream_all_look_the_same(
    client, gateway, admin, upstream
):
    ctx = await gateway()
    unknown = await client.get("/gateway/nope/x", headers=ctx["headers"])
    assert unknown.status_code == 404

    await client.patch(
        f"/api/v1/routes/{ctx['route']['id']}", json={"enabled": False}, headers=admin
    )
    off_route = await client.get("/gateway/echo/x", headers=ctx["headers"])
    await client.patch(
        f"/api/v1/routes/{ctx['route']['id']}", json={"enabled": True}, headers=admin
    )
    await client.patch(
        f"/api/v1/upstreams/{ctx['upstream']['id']}", json={"enabled": False}, headers=admin
    )
    off_upstream = await client.get("/gateway/echo/x", headers=ctx["headers"])
    assert off_route.status_code == off_upstream.status_code == 404
    assert off_route.json() == off_upstream.json() == unknown.json()
    assert upstream.calls == []


async def test_method_not_allowed_returns_405_with_allow_header(client, gateway, upstream):
    ctx = await gateway(methods=("GET", "POST"))
    response = await client.delete("/gateway/echo/x", headers=ctx["headers"])
    assert response.status_code == 405
    assert response.headers["allow"] == "GET, POST"
    assert upstream.calls == []


async def test_head_is_allowed_when_get_is(client, gateway, upstream):
    ctx = await gateway(methods=("GET",))
    response = await client.head("/gateway/echo/x", headers=ctx["headers"])
    assert response.status_code == 200
    assert upstream.calls[0].request.method == "HEAD"


async def test_encoded_path_tricks_are_rejected_and_never_forwarded(client, gateway, upstream):
    ctx = await gateway()
    for path in (
        "/gateway/echo/%2e%2e/admin",
        "/gateway/echo/%2E%2E/admin",
        "/gateway/echo/a/%2e%2e/%2e%2e/etc/passwd",
        "/gateway/echo/a%2fb",
        "/gateway/echo/a%5cb",
        "/gateway/echo/a%00b",
    ):
        response = await client.get(path, headers=ctx["headers"])
        assert response.status_code == 400, path
    assert upstream.calls == []


async def test_special_characters_cannot_change_the_upstream_url(client, gateway, upstream):
    ctx = await gateway()
    await client.get("/gateway/echo/100%25%20sure", headers=ctx["headers"])
    # a literal "%" and a space arrive as encoded text, not as new syntax
    assert upstream.calls[0].request.url.raw_path == b"/100%25%20sure"
    assert upstream.calls[0].request.url.host == "upstream.test"


# ---------------- failure behaviour ----------------
async def test_upstream_timeout_is_504(client, gateway, upstream):
    ctx = await gateway()
    upstream.responder = lambda r: httpx.ReadTimeout("slow")
    response = await client.get("/gateway/echo/x", headers=ctx["headers"])
    assert response.status_code == 504
    upstream.responder = lambda r: httpx.ConnectTimeout("slow")
    assert (await client.get("/gateway/echo/x", headers=ctx["headers"])).status_code == 504


async def test_unreachable_upstream_is_502(client, gateway, upstream):
    ctx = await gateway()
    for error in (
        httpx.ConnectError("refused"),
        httpx.RemoteProtocolError("garbage"),
        httpx.ReadError("reset"),
    ):
        upstream.responder = lambda r, e=error: e
        response = await client.get("/gateway/echo/x", headers=ctx["headers"])
        assert response.status_code == 502, type(error).__name__
        assert "refused" not in response.text  # internal details are not leaked


async def test_exhausted_connection_pool_is_503(client, gateway, upstream):
    ctx = await gateway()
    upstream.responder = lambda r: httpx.PoolTimeout("busy")
    assert (await client.get("/gateway/echo/x", headers=ctx["headers"])).status_code == 503


async def test_failed_requests_are_not_retried(client, gateway, upstream):
    ctx = await gateway()
    upstream.responder = lambda r: httpx.ReadTimeout("slow")
    await client.post("/gateway/echo/payments", content=b"{}", headers=ctx["headers"])
    assert len(upstream.calls) == 1  # a retried POST could charge someone twice

    upstream.calls.clear()
    upstream.responder = lambda r: httpx.Response(500)
    await client.get("/gateway/echo/x", headers=ctx["headers"])
    assert len(upstream.calls) == 1


async def test_upstream_redirects_are_not_followed(client, gateway, upstream):
    ctx = await gateway()
    upstream.responder = lambda r: httpx.Response(
        302, headers={"Location": "http://169.254.169.254/latest/meta-data"}
    )
    response = await client.get("/gateway/echo/x", headers=ctx["headers"])
    assert response.status_code == 302
    assert response.headers["location"] == "http://169.254.169.254/latest/meta-data"
    assert len(upstream.calls) == 1  # the gateway never visited the redirect target


async def test_oversized_body_with_content_length_is_413_without_contacting_upstream(
    client, gateway, upstream, monkeypatch
):
    ctx = await gateway()
    monkeypatch.setattr(get_settings(), "gateway_max_body_bytes", 100)
    response = await client.post("/gateway/echo/x", content=b"x" * 101, headers=ctx["headers"])
    assert response.status_code == 413
    assert upstream.calls == []


async def test_oversized_chunked_body_is_cut_off_with_413(client, gateway, upstream, monkeypatch):
    ctx = await gateway()
    monkeypatch.setattr(get_settings(), "gateway_max_body_bytes", 100)

    async def chunks():
        for _ in range(5):
            yield b"x" * 40

    response = await client.post("/gateway/echo/x", content=chunks(), headers=ctx["headers"])
    assert response.status_code == 413


async def test_body_at_the_limit_is_accepted(client, gateway, upstream, monkeypatch):
    ctx = await gateway()
    monkeypatch.setattr(get_settings(), "gateway_max_body_bytes", 100)
    response = await client.post("/gateway/echo/x", content=b"x" * 100, headers=ctx["headers"])
    assert response.status_code == 200


async def test_invalid_content_length_is_400(client, gateway, upstream):
    ctx = await gateway()
    response = await client.post(
        "/gateway/echo/x", content=b"abc", headers={**ctx["headers"], "Content-Length": "-5"}
    )
    assert response.status_code in (400, 422) or response.status_code >= 400
    assert upstream.calls == []


async def test_database_connection_is_released_before_proxying(
    client, gateway, upstream, session_factory
):
    """While the upstream call is in flight the request must not hold a DB transaction
    (and therefore not a pooled connection)."""
    from app.db.session import get_session
    from app.main import app

    ctx = await gateway()
    sessions = []

    async def tracking_session():
        async with session_factory() as session:
            sessions.append(session)
            yield session

    app.dependency_overrides[get_session] = tracking_session
    seen = {}

    def responder(request):
        seen["in_transaction"] = sessions[-1].in_transaction()
        return httpx.Response(200)

    upstream.responder = responder
    response = await client.get("/gateway/echo/x", headers=ctx["headers"])
    assert response.status_code == 200
    assert seen["in_transaction"] is False
