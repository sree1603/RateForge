"""Prometheus metrics.

Label values are always from small, bounded sets (HTTP method, status class, a *registered*
route prefix, a fixed outcome). Never a path, tenant ID, API key, or anything a client can
invent, because every distinct label value creates a new time series that lives forever.

With several worker processes per container, set PROMETHEUS_MULTIPROC_DIR (to an empty,
writable directory, cleared at start) *before* the app starts so the workers' numbers are
combined. Without it, each scrape sees only whichever worker answered.
"""

import os

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    multiprocess,
)

KNOWN_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})

HTTP_REQUESTS = Counter(
    "rateforge_http_requests_total",
    "All HTTP requests handled, by method and status class.",
    ["method", "status_class"],
)
IN_FLIGHT = Gauge(
    "rateforge_http_requests_in_flight",
    "Requests currently being handled.",
    multiprocess_mode="livesum",
)
GATEWAY_REQUESTS = Counter(
    "rateforge_gateway_requests_total",
    "Gateway requests from authenticated tenants to registered routes.",
    ["route", "outcome", "status_class"],
)
GATEWAY_LATENCY = Histogram(
    "rateforge_gateway_request_duration_seconds",
    "End-to-end gateway latency (receive to last byte sent).",
    ["route", "outcome"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)
UPSTREAM_ERRORS = Counter(
    "rateforge_upstream_errors_total",
    "Failures talking to upstreams.",
    ["kind"],  # timeout | unreachable | pool_exhausted | body_too_large
)
LIMITER_FAILURES = Counter(
    "rateforge_limiter_failures_total", "Times the rate limiter could not reach Redis."
)
DEPENDENCY_ERRORS = Counter(
    "rateforge_dependency_errors_total",
    "Requests answered 503 because a dependency was unreachable.",
    ["dependency"],
)
USAGE_EVENTS = Counter(
    "rateforge_usage_events_total",
    "Usage events by result.",
    ["result"],  # written | dropped | failed
)


def status_class(status_code: int) -> str:
    return f"{status_code // 100}xx"


def observe_request(
    *,
    method: str,
    status_code: int,
    latency_seconds: float,
    route: str | None,
    outcome: str,
) -> None:
    HTTP_REQUESTS.labels(
        method=method if method in KNOWN_METHODS else "OTHER",
        status_class=status_class(status_code),
    ).inc()
    if route is not None:  # only requests to real, registered routes
        GATEWAY_REQUESTS.labels(
            route=route, outcome=outcome, status_class=status_class(status_code)
        ).inc()
        GATEWAY_LATENCY.labels(route=route, outcome=outcome).observe(latency_seconds)


def render() -> tuple[bytes, str]:
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        return generate_latest(registry), CONTENT_TYPE_LATEST
    return generate_latest(), CONTENT_TYPE_LATEST
