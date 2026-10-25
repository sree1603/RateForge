"""Run the load scenarios and record the results with the environment they came from.

Needs the gateway, Postgres, Redis and the echo upstream already running. With Docker Compose:

    docker compose -f compose.yaml -f compose.load.yaml up -d --build
    docker compose -f compose.yaml -f compose.load.yaml run --rm api \
        python scripts/run_load_suite.py --label "1 worker, 1 instance" --machine "<your CPU / RAM>"

Results land in docs/performance-results/<label>/ and docs/performance.md is regenerated.
Use a different --label for every configuration you try.
"""

import argparse
import csv
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "docs" / "performance-results"
SCENARIOS = {
    "baseline": "BaselineUser",
    "allowed": "AllowedUser",
    "limited": "RateLimitedUser",
    "mixed": "MixedUser",
}
MIXED_TENANTS = 20


# ---------------- provisioning (through the public admin API) ----------------
class Admin:
    def __init__(self, url: str, token: str) -> None:
        self.http = httpx.Client(
            base_url=url.rstrip("/"), headers={"X-Admin-Token": token}, timeout=30
        )

    def get_or_create(self, collection: str, name: str, body: dict, key: str = "name") -> dict:
        """Create the item, or return the existing one (matched on field `key`) if it exists."""
        response = self.http.post(f"/api/v1/{collection}", json=body)
        if response.status_code == 201:
            return response.json()
        if response.status_code == 409:
            for item in self.http.get(f"/api/v1/{collection}").json():
                if item[key] == name:
                    return item
        response.raise_for_status()
        raise RuntimeError(f"could not create {collection} {name}: {response.text}")

    def tenant_with_key(self, name: str, plan_id: str) -> str:
        tenant = self.get_or_create("tenants", name, {"name": name})
        self.http.put(
            f"/api/v1/tenants/{tenant['id']}/plan", json={"plan_id": plan_id}
        ).raise_for_status()
        key = self.http.post(
            "/api/v1/keys", json={"tenant_id": tenant["id"], "name": f"load-{int(time.time())}"}
        )
        key.raise_for_status()
        return key.json()["api_key"]


def provision(admin: Admin, upstream_url: str) -> dict:
    def plan(name: str, per_window: int, window: int) -> str:
        body = {"name": name, "requests_per_window": per_window, "window_seconds": window}
        return admin.get_or_create("plans", name, body)["id"]

    unlimited = plan("load-unlimited", 1_000_000, 60)  # never the limiting factor
    limited = plan("load-limited", 10, 3600)  # 10 requests, then 429 for an hour
    mixed = plan("load-mixed", 100, 10)  # burst 100, then ~10 requests/second per tenant

    upstream = admin.get_or_create(
        "upstreams", "load-echo", {"name": "load-echo", "base_url": upstream_url}
    )
    admin.get_or_create(
        "routes",
        "load",
        {"upstream_id": upstream["id"], "path_prefix": "load", "allowed_methods": ["GET"]},
        key="path_prefix",  # routes are identified by their prefix, not a name
    )
    return {
        "route": "load",
        "keys": {
            "unlimited": admin.tenant_with_key("load-unlimited", unlimited),
            "limited": admin.tenant_with_key("load-limited", limited),
            "mixed": [
                admin.tenant_with_key(f"load-mixed-{i}", mixed) for i in range(MIXED_TENANTS)
            ],
        },
    }


# ---------------- running locust ----------------
def run_scenario(name: str, args, config_path: str, out_dir: Path) -> dict:
    urls = [u.strip() for u in args.gateway_urls.split(",") if u.strip()]
    host = args.direct_upstream_url if name == "baseline" else urls[0]
    command = [
        sys.executable, "-m", "locust",
        "-f", str(ROOT / "tests" / "load" / "locustfile.py"),
        "--headless", "-u", str(args.users), "-r", str(args.spawn_rate),
        "-t", f"{args.duration}s", "--host", host,
        "--csv", str(out_dir / name), "--only-summary", "--loglevel", "WARNING",
        SCENARIOS[name],
    ]  # fmt: skip
    env = {
        **os.environ,
        "LOAD_CONFIG": config_path,
        "GATEWAY_URLS": args.gateway_urls,
        "DIRECT_UPSTREAM_URL": args.direct_upstream_url,
    }
    print(f"\n=== {name}: {args.users} users, {args.duration}s ===", flush=True)
    subprocess.run(command, check=False, env=env)
    return read_stats(out_dir / f"{name}_stats.csv")


def read_stats(path: Path) -> dict:
    """Aggregate numbers plus the share of each response status, from Locust's CSV."""
    with open(path, newline="") as handle:
        rows = list(csv.DictReader(handle))
    total = next(r for r in rows if r["Name"] == "Aggregated")
    statuses: dict[str, dict] = {}
    for row in rows:
        match = re.search(r"\[(\d+)\]$", row["Name"])
        if match:
            statuses[match.group(1)] = {
                "requests": int(row["Request Count"]),
                "p50": float(row["50%"]),
                "p95": float(row["95%"]),
                "p99": float(row["99%"]),
            }
    return {
        "requests": int(total["Request Count"]),
        "failures": int(total["Failure Count"]),
        "rps": float(total["Requests/s"]),
        "p50": float(total["50%"]),
        "p95": float(total["95%"]),
        "p99": float(total["99%"]),
        "statuses": statuses,
    }


def environment(args) -> dict:
    memory_gb = None
    try:
        with open("/proc/meminfo") as handle:
            memory_gb = round(int(handle.readline().split()[1]) / 1024 / 1024, 1)
    except OSError:
        pass
    return {
        "machine": args.machine,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpus_visible": os.cpu_count(),
        "memory_gb_visible": memory_gb,
        "users": args.users,
        "duration_seconds": args.duration,
        "gateway_instances": len([u for u in args.gateway_urls.split(",") if u.strip()]),
        "note": args.note,
        "date": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "--label", required=True, help="name of this configuration, e.g. '2 workers'"
    )
    parser.add_argument("--machine", default="unspecified", help="describe the host CPU/RAM/OS")
    parser.add_argument(
        "--note", default="", help="anything else worth recording (settings changed)"
    )
    parser.add_argument("--admin-url", default="http://api:8000")
    parser.add_argument("--gateway-urls", default="http://api:8000", help="comma separated")
    parser.add_argument("--upstream-url", default="http://echo:9000", help="as the gateway sees it")
    parser.add_argument(
        "--direct-upstream-url", default="http://echo:9000", help="as this script sees it"
    )
    parser.add_argument("--admin-token", default=os.environ.get("ADMIN_TOKEN", ""))
    parser.add_argument("--users", type=int, default=50)
    parser.add_argument("--spawn-rate", type=int, default=25)
    parser.add_argument("--duration", type=int, default=30, help="seconds per scenario")
    parser.add_argument("--scenarios", default=",".join(SCENARIOS))
    args = parser.parse_args()

    out_dir = RESULTS / re.sub(r"[^A-Za-z0-9._-]+", "-", args.label).strip("-")
    out_dir.mkdir(parents=True, exist_ok=True)

    print("Provisioning tenants, plans, keys and a route...", flush=True)
    config = provision(Admin(args.admin_url, args.admin_token), args.upstream_url)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(config, handle)
        config_path = handle.name

    results = {}
    try:
        for name in [s.strip() for s in args.scenarios.split(",") if s.strip()]:
            results[name] = run_scenario(name, args, config_path, out_dir)
    finally:
        os.unlink(config_path)  # it contains API keys

    (out_dir / "meta.json").write_text(
        json.dumps(
            {"label": args.label, "environment": environment(args), "results": results}, indent=2
        )
    )
    from summarize_load import write_report

    write_report(RESULTS, ROOT / "docs" / "performance.md")
    print(f"\nWrote {out_dir.relative_to(ROOT)}/ and docs/performance.md")


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
