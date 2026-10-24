"""How the gateway hands facts about a request to the middleware that records usage.

The middleware creates a `UsageContext` before the request runs and publishes it in a
ContextVar. The gateway handler fills it in as it learns things (which tenant, which route,
what happened). When the response has finished, the middleware turns it into a `UsageRecord`
and gives it to the recorder. This way the recorded latency and status are the real,
end-to-end ones, including time spent streaming the body back to the client.

Only fixed, low-cardinality facts are carried here: never headers, query strings, bodies, API
keys, or the request path beyond the route prefix.
"""

import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime

# What happened to a request:
#   forwarded       passed the limiter and was sent to the upstream (status may still be 5xx)
#   rate_limited    rejected by the per-window limit (429)
#   quota_exceeded  rejected by the monthly quota (429)
#   rejected        refused by the gateway for another reason (405, 400, 403 no plan, 503 limiter)
OUTCOMES = ("forwarded", "rate_limited", "quota_exceeded", "rejected")


@dataclass
class UsageContext:
    tenant_id: uuid.UUID | None = None
    route_id: uuid.UUID | None = None
    route_prefix: str | None = None
    outcome: str = "rejected"

    @property
    def recordable(self) -> bool:
        """Only requests from an authenticated tenant to a real, enabled route are recorded.

        Unauthenticated requests and unknown prefixes are not: they have no tenant to attribute
        to, and recording attacker-chosen route names would let anyone bloat the table.
        """
        return self.tenant_id is not None and self.route_prefix is not None


@dataclass(frozen=True)
class UsageRecord:
    tenant_id: uuid.UUID
    route_id: uuid.UUID | None
    route_prefix: str
    request_id: str
    method: str
    status_code: int
    outcome: str
    latency_ms: float
    occurred_at: datetime


usage_ctx: ContextVar[UsageContext | None] = ContextVar("usage_ctx", default=None)
