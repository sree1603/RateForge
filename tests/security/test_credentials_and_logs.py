import json
import logging

import httpx
import pytest

from app.core.config import get_settings
from app.core.logging import JsonFormatter
from app.core.middleware import _SAFE_REQUEST_ID


# ---------------- credential handling ----------------
def _fuzzed_keys(valid: str) -> list[str]:
    prefix, secret = valid.split("_", 2)[1:]
    return [
        "",
        " ",
        "rf",
        "rf_",
        "rf__",
        "rf_" + "0" * 12 + "_",
        f"rf_{prefix}",
        f"rf_{prefix}_",
        f"rf_{prefix}_{secret[:-1]}",  # one character short
        f"rf_{prefix}_{secret}x",  # one character long
        f"rf_{prefix}_{secret.swapcase()}",
        f"RF_{prefix}_{secret}",
        f"rf_{prefix.upper()}_{secret}",
        f" {valid}",
        f"{valid} ",
        f"Bearer {valid}",
        valid + valid,
        "rf_" + "f" * 12 + "_" + "A" * 43,
        "a" * 100_000,
        "rf_" + "é" * 12 + "_x",
        "' OR '1'='1",
        "../../etc/passwd",
        "%00",
        "null",
        "undefined",
        "0",
    ]


async def test_every_malformed_key_gets_the_same_401(client, gateway, make_key):
    ctx = await gateway()
    valid = ctx["headers"]["X-API-Key"]
    bodies = set()
    for candidate in _fuzzed_keys(valid):
        try:
            response = await client.get("/api/v1/keys", headers={"X-API-Key": candidate})
        except (httpx.InvalidURL, UnicodeEncodeError, httpx.LocalProtocolError):
            continue  # the client library itself refused to send it
        assert response.status_code == 401, repr(candidate[:40])
        bodies.add(response.text)
    assert len(bodies) == 1  # nothing in the response tells an attacker how close they got


async def test_wrong_admin_tokens_all_get_the_same_401(client):
    real = get_settings().admin_token
    bodies = set()
    for candidate in (
        "",
        "x",
        real[:-1],
        real + "x",
        real.upper(),
        real[::-1],
        "admin",
        "a" * 5000,
    ):
        response = await client.get("/api/v1/tenants", headers={"X-Admin-Token": candidate})
        assert response.status_code == 401, candidate[:20]
        bodies.add(response.text)
    assert len(bodies) == 1


async def test_a_key_works_only_on_the_gateway_data_plane_and_own_data(client, gateway):
    ctx = await gateway()
    headers = ctx["headers"]
    for path in (
        "/api/v1/tenants",
        "/api/v1/plans",
        "/api/v1/upstreams",
        "/api/v1/routes",
        "/metrics",
    ):
        assert (await client.get(path, headers=headers)).status_code in (401, 403), path
    assert (
        await client.post("/api/v1/tenants", json={"name": "x"}, headers=headers)
    ).status_code == 403


# ---------------- request IDs are not an injection channel ----------------
@pytest.mark.parametrize("good", ["abc", "req_1234", "a.b-c_d", "A" * 64, "0"])
def test_ordinary_request_ids_are_accepted(good):
    assert _SAFE_REQUEST_ID.match(good)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "has space",
        "semi;colon",
        "a" * 65,
        "x\r\nSet-Cookie: a=1",
        "x\nX-Evil: 1",
        "<script>",
        "a/b",
        "é",
        "a\x00b",
    ],
)
def test_unsafe_request_ids_are_not_accepted(bad):
    assert not _SAFE_REQUEST_ID.match(bad)


async def test_unsafe_inbound_request_ids_are_replaced_not_reflected(client, gateway, upstream):
    ctx = await gateway()
    for bad in ("has space", "<script>alert(1)</script>", "a" * 65, "x;y"):
        response = await client.get(
            "/gateway/echo/x", headers={**ctx["headers"], "X-Request-ID": bad}
        )
        assert response.headers["x-request-id"].startswith("req_")
        assert bad not in str(response.headers)
        assert upstream.calls[-1].request.headers["x-request-id"].startswith("req_")


# ---------------- logs never contain secrets ----------------
class _EmitTimeJsonCapture(logging.Handler):
    """Renders each record the moment it is emitted, as production does. (Formatting later
    would miss the request ID, which only exists while the request is running.)"""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.setFormatter(JsonFormatter())
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


@pytest.fixture
def json_logs():
    handler = _EmitTimeJsonCapture()
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    yield handler
    root.removeHandler(handler)
    root.setLevel(previous_level)


async def test_api_keys_and_tokens_never_appear_in_logs(
    client, gateway, upstream, admin, json_logs, db
):
    from sqlalchemy import update

    from app.models import Tenant

    ctx = await gateway(limit=2, window=3600)
    key = ctx["headers"]["X-API-Key"]
    secret_part = key.split("_", 2)[2]
    bad_key = "rf_" + "a" * 12 + "_" + "LEAKCHECKBADKEY" + "z" * 28

    await client.get("/gateway/echo/ok", headers=ctx["headers"])  # success
    await client.get("/gateway/echo/ok", headers=ctx["headers"])  # success
    await client.get("/gateway/echo/limited", headers=ctx["headers"])  # 429
    await client.get("/gateway/echo/x", headers={"X-API-Key": bad_key})  # invalid key
    upstream.responder = lambda r: httpx.ConnectError(f"refused, key was {key}")
    await ctx["assign_plan"](ctx["tenant"]["id"], 100, 60, None)
    await client.get("/gateway/echo/fail", headers=ctx["headers"])  # upstream failure
    await client.post(
        "/api/v1/keys", json={"name": "extra", "tenant_id": ctx["tenant"]["id"]}, headers=admin
    )
    await client.delete(
        f"/api/v1/keys/{(await client.get('/api/v1/keys', headers=admin)).json()[0]['id']}",
        headers=admin,
    )
    await db.execute(update(Tenant).values(status="suspended"))
    await db.commit()
    await client.get("/gateway/echo/x", headers=ctx["headers"])  # suspended tenant

    everything = "\n".join(json_logs.lines)
    assert len(json_logs.lines) > 5  # we really did capture the requests' log lines
    for secret in (
        key,
        secret_part,
        bad_key,
        "LEAKCHECKBADKEY",
        get_settings().admin_token,
        get_settings().api_key_hmac_secret,
    ):
        assert secret not in everything
    assert all(json.loads(line) for line in json_logs.lines)  # every line is valid JSON


async def test_every_log_line_for_a_request_carries_its_request_id(client, gateway, json_logs):
    ctx = await gateway()
    response = await client.get("/gateway/echo/x", headers=ctx["headers"])
    rid = response.headers["x-request-id"]
    lines = [json.loads(line) for line in json_logs.lines]
    completed = [line for line in lines if line["message"] == "request completed"]
    assert any(line["request_id"] == rid and line["status_code"] == 200 for line in completed)
