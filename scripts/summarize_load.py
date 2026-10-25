"""Turn the recorded load-test runs into docs/performance.md.

python scripts/summarize_load.py
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

INTRO = """# Performance results

Everything below was **measured**, not estimated. Each row comes from a run of
`scripts/run_load_suite.py`; the raw Locust CSV files are next to this document in
`performance-results/<label>/`.

How to read these numbers:

- Users send requests back to back with no think time, so this is closer to a *maximum* than to
  typical traffic.
- The load generator, gateway, PostgreSQL, Redis and the upstream all share one machine in these
  runs, so they compete for the same CPU. A dedicated load-generator machine would show higher
  numbers. Treat the figures as a baseline for *this* setup, and compare configurations only
  against each other.
- Latency is end to end as seen by the load generator, in milliseconds.
- "baseline" calls the upstream directly. The gap between it and "allowed" is the gateway's
  overhead (authentication, route lookup, rate limiting, proxying, usage recording).
- Runs whose machine is described as a **development sandbox** (1 CPU, shared by everything) are
  reference runs that validate the harness and the A/B comparisons. They are *not* what this
  software can do on real hardware. Add your own runs and compare configurations against each other.
- In "limited" and "mixed", HTTP 429 is an expected answer, not a failure. "Failures" counts only
  unexpected responses (5xx, timeouts, and so on).
"""


def _fmt(value: float) -> str:
    return f"{value:,.0f}" if value >= 100 else f"{value:,.1f}"


def _share(result: dict) -> str:
    total = result["requests"] or 1
    parts = [
        f"{code}: {100 * s['requests'] / total:.0f}%"
        for code, s in sorted(result["statuses"].items())
    ]
    return ", ".join(parts) if parts else "-"


def write_report(results_dir: Path, output: Path) -> None:
    runs = []
    for meta_file in sorted(results_dir.glob("*/meta.json")):
        runs.append(json.loads(meta_file.read_text()))
    lines = [INTRO]
    if not runs:
        lines.append("\n_No runs recorded yet. Run `scripts/run_load_suite.py` to create some._\n")
    for run in runs:
        env = run["environment"]
        lines.append(f"\n## {run['label']}\n")
        details = [
            f"machine: {env['machine']}",
            f"visible to the test container: {env['cpus_visible']} CPUs"
            + (f", {env['memory_gb_visible']} GB RAM" if env.get("memory_gb_visible") else ""),
            f"{env['users']} concurrent users, {env['duration_seconds']} s per scenario",
            f"{env['gateway_instances']} gateway instance(s)",
            f"date: {env['date']}",
        ]
        if env.get("note"):
            details.append(f"notes: {env['note']}")
        lines.append("- " + "\n- ".join(details) + "\n")
        lines.append("| Scenario | Requests/s | p50 ms | p95 ms | p99 ms | Responses | Failures |")
        lines.append("|---|---:|---:|---:|---:|---|---:|")
        for name in ("baseline", "allowed", "limited", "mixed"):
            result = run["results"].get(name)
            if result is None:
                continue
            lines.append(
                f"| {name} | {_fmt(result['rps'])} | {_fmt(result['p50'])} | {_fmt(result['p95'])} "
                f"| {_fmt(result['p99'])} | {_share(result)} | {result['failures']} |"
            )
    output.write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    write_report(ROOT / "docs" / "performance-results", ROOT / "docs" / "performance.md")
    print("Wrote docs/performance.md")
