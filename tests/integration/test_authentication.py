async def test_missing_credentials_is_401(client):
    response = await client.get("/api/v1/keys")
    assert response.status_code == 401


async def test_all_bad_keys_get_the_same_response(client, make_tenant, make_key):
    tenant = await make_tenant()
    good = (await make_key(tenant["id"]))["api_key"]
    prefix, secret = good.split("_", 2)[1:]

    attempts = {
        "garbage": "not-a-key",
        "unknown prefix": f"rf_{'0' * 12}_{secret}",
        "wrong secret": f"rf_{prefix}_{'x' * len(secret)}",
        "truncated": good[:-5],
        "extra suffix": good + "zzz",
        "empty": " ",
    }
    bodies = set()
    for label, key in attempts.items():
        response = await client.get("/api/v1/keys", headers={"X-API-Key": key})
        assert response.status_code == 401, label
        bodies.add(response.text)
    assert len(bodies) == 1  # callers cannot tell why a key failed


async def test_admin_token_wins_but_wrong_admin_token_does_not_fall_back_to_key(
    client, make_tenant, make_key
):
    tenant = await make_tenant()
    key = await make_key(tenant["id"])
    response = await client.get(
        "/api/v1/keys", headers={"X-Admin-Token": "wrong", "X-API-Key": key["api_key"]}
    )
    assert response.status_code == 401


async def test_key_is_not_leaked_in_error_responses(client):
    secret_looking = "rf_0123456789ab_THIS-MUST-NOT-ECHO"
    response = await client.get("/api/v1/keys", headers={"X-API-Key": secret_looking})
    assert "THIS-MUST-NOT-ECHO" not in response.text
