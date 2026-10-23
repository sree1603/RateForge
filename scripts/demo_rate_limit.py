"""Show that the rate limit is shared by two gateway instances.

Sends requests alternately to two gateways that use the same Redis, and counts how many were
allowed. If limits were kept in each instance's memory, you would see about twice the plan limit
get through. With the shared Redis bucket the total stays at the plan limit, plus the few
tokens that refill while the script runs (a Basic plan earns 1 token per second).

    docker compose run --rm api python scripts/demo_rate_limit.py --key rf_... --requests 100
"""

import argparse
import asyncio
import time
from collections import Counter

import httpx


async def main(key: str, urls: list[str], route: str, requests: int, window: int) -> None:
    per_instance: dict[str, Counter] = {url: Counter() for url in urls}
    total: Counter = Counter()
    headers = {"X-API-Key": key}
    limit = 0
    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=10) as client:
        for i in range(requests):
            url = urls[i % len(urls)]
            response = await client.get(f"{url}/gateway/{route}/demo", headers=headers)
            per_instance[url][response.status_code] += 1
            total[response.status_code] += 1
            if i == 0:
                limit = int(response.headers.get("x-ratelimit-limit", "0"))
                print(
                    f"first response: {response.status_code}, "
                    f"X-RateLimit-Limit={response.headers.get('x-ratelimit-limit')}, "
                    f"X-RateLimit-Remaining={response.headers.get('x-ratelimit-remaining')}"
                )
    elapsed = time.perf_counter() - started
    print()
    for url, counts in per_instance.items():
        print(f"{url:<24} 200 OK: {counts[200]:>4}    429 rejected: {counts[429]:>4}")
    print(f"{'TOTAL':<24} 200 OK: {total[200]:>4}    429 rejected: {total[429]:>4}")
    if limit:
        refilled = limit / window * elapsed
        print(
            f"\nThe run took {elapsed:.1f}s. Plan limit {limit} per {window}s, so about "
            f"{refilled:.0f} tokens refilled meanwhile: expect roughly "
            f"{limit + refilled:.0f} allowed."
        )
        print(f"Per-instance counters would have allowed about {limit * len(urls)}.")
    other = {code: n for code, n in total.items() if code not in (200, 429)}
    if other:
        print(f"other statuses: {other}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--key", required=True, help="tenant API key")
    parser.add_argument("--urls", default="http://api:8000,http://api2:8000")
    parser.add_argument("--route", default="echo")
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--window", type=int, default=60, help="the plan's window in seconds")
    args = parser.parse_args()
    asyncio.run(main(args.key, args.urls.split(","), args.route, args.requests, args.window))
