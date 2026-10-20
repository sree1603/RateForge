import uuid


async def _upstream(client, admin, **overrides):
    body = {"name": "echo", "base_url": "http://echo:9000", **overrides}
    return await client.post("/api/v1/upstreams", json=body, headers=admin)


async def test_upstream_and_route_endpoints_are_admin_only(client, make_tenant, make_key):
    for method, url in (("GET", "/api/v1/upstreams"), ("GET", "/api/v1/routes")):
        assert (await client.request(method, url)).status_code == 401
    tenant = await make_tenant()
    key = await make_key(tenant["id"])
    forbidden = await client.get("/api/v1/upstreams", headers={"X-API-Key": key["api_key"]})
    assert forbidden.status_code == 403


async def test_create_upstream_normalises_url_and_applies_defaults(client, admin):
    response = await _upstream(client, admin, base_url="http://echo:9000/")
    assert response.status_code == 201
    body = response.json()
    assert body["base_url"] == "http://echo:9000"
    assert body["enabled"] is True
    assert body["connect_timeout_seconds"] == 3.0
    assert body["read_timeout_seconds"] == 30.0


async def test_dangerous_upstream_urls_are_rejected_with_422(client, admin):
    for url in (
        "http://169.254.169.254/latest/meta-data",
        "http://2852039166",
        "ftp://example.com",
        "http://user:pw@example.com",
        "not a url",
    ):
        response = await _upstream(client, admin, name="bad", base_url=url)
        assert response.status_code == 422, url


async def test_timeout_bounds_are_validated(client, admin):
    assert (await _upstream(client, admin, connect_timeout_seconds=0)).status_code == 422
    assert (await _upstream(client, admin, read_timeout_seconds=9999)).status_code == 422


async def test_duplicate_upstream_name_is_409(client, admin):
    assert (await _upstream(client, admin)).status_code == 201
    assert (await _upstream(client, admin)).status_code == 409


async def test_route_requires_existing_upstream(client, admin):
    response = await client.post(
        "/api/v1/routes",
        json={"upstream_id": str(uuid.uuid4()), "path_prefix": "x"},
        headers=admin,
    )
    assert response.status_code == 404


async def test_route_validation(client, admin):
    up = (await _upstream(client, admin)).json()

    async def make(**fields):
        return await client.post(
            "/api/v1/routes", json={"upstream_id": up["id"], **fields}, headers=admin
        )

    for bad_prefix in ("", "Has-Upper", "has space", "-leading", "a/b", "..", "x" * 65):
        assert (await make(path_prefix=bad_prefix)).status_code == 422, bad_prefix
    assert (await make(path_prefix="ok", allowed_methods=[])).status_code == 422
    assert (await make(path_prefix="ok", allowed_methods=["GET", "TRACE"])).status_code == 422
    assert (await make(path_prefix="ok", allowed_methods=["CONNECT"])).status_code == 422


async def test_route_methods_are_normalised_and_deduplicated(client, admin):
    up = (await _upstream(client, admin)).json()
    response = await client.post(
        "/api/v1/routes",
        json={
            "upstream_id": up["id"],
            "path_prefix": "ok",
            "allowed_methods": ["get", "GET", "post"],
        },
        headers=admin,
    )
    assert response.status_code == 201
    assert response.json()["allowed_methods"] == ["GET", "POST"]
    assert response.json()["strip_prefix"] is True


async def test_duplicate_route_prefix_is_409(client, admin):
    up = (await _upstream(client, admin)).json()
    body = {"upstream_id": up["id"], "path_prefix": "ok"}
    assert (await client.post("/api/v1/routes", json=body, headers=admin)).status_code == 201
    assert (await client.post("/api/v1/routes", json=body, headers=admin)).status_code == 409


async def test_list_and_toggle(client, admin):
    up = (await _upstream(client, admin)).json()
    route = (
        await client.post(
            "/api/v1/routes", json={"upstream_id": up["id"], "path_prefix": "ok"}, headers=admin
        )
    ).json()

    off = await client.patch(
        f"/api/v1/routes/{route['id']}", json={"enabled": False}, headers=admin
    )
    assert off.json()["enabled"] is False
    off_up = await client.patch(
        f"/api/v1/upstreams/{up['id']}", json={"enabled": False}, headers=admin
    )
    assert off_up.json()["enabled"] is False
    assert [r["enabled"] for r in (await client.get("/api/v1/routes", headers=admin)).json()] == [
        False
    ]
    assert [
        u["enabled"] for u in (await client.get("/api/v1/upstreams", headers=admin)).json()
    ] == [False]

    missing = str(uuid.uuid4())
    assert (
        await client.patch(f"/api/v1/routes/{missing}", json={"enabled": True}, headers=admin)
    ).status_code == 404
    assert (
        await client.patch(f"/api/v1/upstreams/{missing}", json={"enabled": True}, headers=admin)
    ).status_code == 404
