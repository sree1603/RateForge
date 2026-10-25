"""End-to-end check of a running stack, through the real HTTP interface.

    docker compose run --rm api python scripts/smoke_test.py --key <API_KEY>

Exits non-zero (and says which check failed) if anything is wrong. CI runs it after starting the
containers; you can run it any time you change something.
"""

import argparse
import os
import sys
import time

import httpx

FAILED: list[str] = []


def check(description: str, condition: bool, detail: str = "") -> None:
    print(
        f"  {'PASS' if condition else 'FAIL'}  {description}"
        + (f"  ({detail})" if detail and not condition else "")
    )
    if not condition:
        FAILED.append(description)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://api:8000")
    parser.add_argument("--key", required=True, help="a tenant API key with a plan")
    parser.add_argument("--route", default="echo")
    parser.add_argument("--admin-token", default=os.environ.get("ADMIN_TOKEN", ""))
    args = parser.parse_args()

    client = httpx.Client(base_url=args.base_url.rstrip("/"), timeout=15)
    key = {"X-API-Key": args.key}
    print(f"Smoke test against {args.base_url}")

    check("liveness answers 200", client.get("/health/live").status_code == 200)
    ready = client.get("/health/ready")
    check("readiness: Postgres and Redis reachable", ready.status_code == 200, ready.text)

    check(
        "gateway refuses a request with no key (401)",
        client.get(f"/gateway/{args.route}/x").status_code == 401,
    )
    check(
        "gateway refuses a made-up key (401)",
        client.get(
            f"/gateway/{args.route}/x", headers={"X-API-Key": "rf_000000000000_nope"}
        ).status_code
        == 401,
    )

    forwarded = client.get(f"/gateway/{args.route}/hello?x=1", headers=key)
    check(
        "authenticated request is forwarded (200)",
        forwarded.status_code == 200,
        forwarded.text[:120],
    )
    check(
        "response carries X-Request-ID",
        forwarded.headers.get("x-request-id", "").startswith("req_"),
    )
    check("response carries rate-limit headers", "x-ratelimit-remaining" in forwarded.headers)
    if forwarded.status_code == 200 and "headers" in forwarded.text:
        seen = forwarded.json()
        check(
            "upstream saw the right path and query",
            seen.get("path") == "/hello" and seen.get("query") == "x=1",
        )
        check(
            "API key was not forwarded to the upstream", "x-api-key" not in seen.get("headers", {})
        )

    check(
        "path traversal is refused (400)",
        client.get(f"/gateway/{args.route}/%2e%2e/admin", headers=key).status_code == 400,
    )
    check(
        "unknown route is 404",
        client.get("/gateway/no-such-route/x", headers=key).status_code == 404,
    )

    check("/metrics is not public (401)", client.get("/metrics").status_code == 401)
    if args.admin_token:
        metrics = client.get("/metrics", headers={"X-Admin-Token": args.admin_token})
        check(
            "/metrics works with the admin token",
            metrics.status_code == 200 and "rateforge_" in metrics.text,
        )

    # Usage events are written in batches, so give the recorder a moment.
    total = 0
    for _ in range(15):
        summary = client.get("/api/v1/usage/summary", headers=key)
        total = summary.json().get("total_requests", 0) if summary.status_code == 200 else 0
        if total >= 1:
            break
        time.sleep(0.5)
    check("usage analytics recorded the requests", total >= 1, f"total_requests={total}")

    print()
    if FAILED:
        print(f"{len(FAILED)} check(s) failed:")
        for description in FAILED:
            print(f"  - {description}")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    main()
