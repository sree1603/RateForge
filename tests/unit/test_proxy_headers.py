from app.services.proxy import filter_request_headers, filter_response_headers


def _raw(*pairs):
    return [(k.encode(), v.encode()) for k, v in pairs]


def _request(*pairs, **kwargs):
    defaults = {"request_id": "req_1", "tenant_id": "tenant-1", "client_ip": "203.0.113.9"}
    return dict(
        filter_request_headers(_raw(*pairs), **{**defaults, **kwargs})
    ), filter_request_headers(_raw(*pairs), **{**defaults, **kwargs})


def test_hop_by_hop_request_headers_are_dropped():
    headers, _ = _request(
        ("Connection", "keep-alive"),
        ("Keep-Alive", "timeout=5"),
        ("Transfer-Encoding", "chunked"),
        ("Upgrade", "websocket"),
        ("TE", "trailers"),
        ("Proxy-Authorization", "Basic abc"),
        ("Accept", "application/json"),
    )
    for name in (
        "connection",
        "keep-alive",
        "transfer-encoding",
        "upgrade",
        "te",
        "proxy-authorization",
    ):
        assert name not in headers
    assert headers["accept"] == "application/json"


def test_headers_named_in_connection_are_dropped_too():
    headers, _ = _request(
        ("Connection", "close, X-Secret-Hop"), ("X-Secret-Hop", "1"), ("X-Keep", "2")
    )
    assert "x-secret-hop" not in headers
    assert headers["x-keep"] == "2"


def test_credentials_for_the_gateway_are_never_forwarded():
    headers, _ = _request(
        ("X-API-Key", "rf_secret"), ("X-Admin-Token", "admin"), ("Host", "gateway.local")
    )
    assert "x-api-key" not in headers
    assert "x-admin-token" not in headers
    assert "host" not in headers


def test_upstream_credentials_are_forwarded():
    headers, _ = _request(("Authorization", "Bearer upstream-token"))
    assert headers["authorization"] == "Bearer upstream-token"


def test_spoofed_gateway_headers_are_replaced():
    headers, _ = _request(("X-RateForge-Tenant", "someone-else"), ("X-Request-ID", "forged"))
    assert headers["x-rateforge-tenant"] == "tenant-1"
    assert headers["x-request-id"] == "req_1"


def test_forwarded_for_is_appended():
    headers, _ = _request(("X-Forwarded-For", "198.51.100.1"))
    assert headers["x-forwarded-for"] == "198.51.100.1, 203.0.113.9"
    headers, _ = _request()
    assert headers["x-forwarded-for"] == "203.0.113.9"


def test_duplicate_headers_are_preserved():
    _, pairs = _request(("Cookie", "a=1"), ("Cookie", "b=2"))
    assert [v for k, v in pairs if k == "cookie"] == ["a=1", "b=2"]


def test_identity_encoding_added_only_when_client_sent_none():
    headers, _ = _request()
    assert headers["accept-encoding"] == "identity"
    headers, _ = _request(("Accept-Encoding", "gzip"))
    assert headers["accept-encoding"] == "gzip"


def test_response_hop_by_hop_headers_dropped_end_to_end_ones_kept():
    out = dict(
        filter_response_headers(
            _raw(
                ("Connection", "keep-alive, X-Internal"),
                ("Keep-Alive", "timeout=5"),
                ("Transfer-Encoding", "chunked"),
                ("X-Internal", "1"),
                ("Content-Type", "application/json"),
                ("Content-Length", "12"),
                ("X-Custom", "yes"),
            )
        )
    )
    assert set(out) == {b"content-type", b"content-length", b"x-custom"}


def test_response_keeps_multiple_set_cookie_headers():
    out = filter_response_headers(_raw(("Set-Cookie", "a=1"), ("Set-Cookie", "b=2")))
    assert [v for k, v in out if k == b"set-cookie"] == [b"a=1", b"b=2"]
