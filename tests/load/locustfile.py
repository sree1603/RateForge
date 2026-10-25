"""Load scenarios for RateForge. Run through scripts/run_load_suite.py, which provisions the
tenants and keys these users need and writes the config file named by LOAD_CONFIG.

    BaselineUser      calls the upstream directly: what the upstream alone can do, so the
                      gateway's own overhead can be read off by comparison
    AllowedUser       one tenant with a huge limit: every request is forwarded (the "happy path")
    RateLimitedUser   one tenant that is out of tokens: nearly every request is a 429, so this
                      measures the cost of rejecting (auth + Redis, no upstream call)
    MixedUser         many tenants with modest limits: a realistic blend of 200s and 429s

Users send requests back to back (no think time), so the numbers are closer to a maximum than
to typical load.
"""

import json
import os
import random

from locust import HttpUser, constant, task


def _load_config() -> dict:
    path = os.environ.get("LOAD_CONFIG")
    if not path:
        raise RuntimeError("LOAD_CONFIG is not set; run this through scripts/run_load_suite.py")
    with open(path) as handle:
        return json.load(handle)


CONFIG = _load_config()
GATEWAYS = [u.strip().rstrip("/") for u in os.environ["GATEWAY_URLS"].split(",") if u.strip()]
ROUTE = CONFIG["route"]
DIRECT_UPSTREAM = os.environ.get("DIRECT_UPSTREAM_URL", "").rstrip("/")


class _GatewayUser(HttpUser):
    abstract = True
    wait_time = constant(0)
    expected_statuses = {200}

    def on_start(self) -> None:
        self._turn = random.randrange(len(GATEWAYS))  # spread users across gateway instances
        self.api_key = self.pick_key()

    def pick_key(self) -> str:
        raise NotImplementedError

    @task
    def call_gateway(self) -> None:
        base = GATEWAYS[self._turn % len(GATEWAYS)]
        self._turn += 1
        with self.client.get(
            f"{base}/gateway/{ROUTE}/fast",
            headers={"X-API-Key": self.api_key},
            name="gateway",
            catch_response=True,
        ) as response:
            # Report each status separately, so 200s and 429s have their own latency numbers.
            response.request_meta["name"] = f"gateway [{response.status_code}]"
            if response.status_code in self.expected_statuses:
                response.success()
            else:
                response.failure(f"unexpected status {response.status_code}")


class AllowedUser(_GatewayUser):
    expected_statuses = {200}

    def pick_key(self) -> str:
        return CONFIG["keys"]["unlimited"]


class RateLimitedUser(_GatewayUser):
    expected_statuses = {200, 429}  # the tenant's bucket is exhausted on purpose

    def pick_key(self) -> str:
        return CONFIG["keys"]["limited"]


class MixedUser(_GatewayUser):
    expected_statuses = {200, 429}

    def pick_key(self) -> str:
        return random.choice(CONFIG["keys"]["mixed"])


class BaselineUser(HttpUser):
    wait_time = constant(0)

    @task
    def call_upstream(self) -> None:
        with self.client.get(
            f"{DIRECT_UPSTREAM}/fast", name="direct upstream", catch_response=True
        ) as response:
            response.request_meta["name"] = f"direct upstream [{response.status_code}]"
            if response.status_code == 200:
                response.success()
            else:
                response.failure(f"unexpected status {response.status_code}")
