import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class UsageEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    tenant_id: uuid.UUID
    route_prefix: str
    request_id: str
    method: str
    status_code: int
    outcome: str
    latency_ms: float
    occurred_at: datetime


class UsagePage(BaseModel):
    items: list[UsageEventOut]
    next_cursor: str | None = None


class RejectedCounts(BaseModel):
    total: int
    rate_limited: int
    quota_exceeded: int
    other: int


class LatencyStats(BaseModel):
    avg: float | None
    p50: float | None
    p95: float | None
    p99: float | None


class RouteStats(BaseModel):
    route: str
    requests: int
    errors: int
    error_rate: float | None
    p95_latency_ms: float | None


class TimelinePoint(BaseModel):
    bucket: datetime
    requests: int
    rejected: int
    errors: int


class UsageSummary(BaseModel):
    start: datetime
    end: datetime
    bucket: str
    total_requests: int
    accepted: int
    rejected: RejectedCounts
    status_classes: dict[str, int]
    error_rate: float | None
    latency_ms: LatencyStats
    top_routes: list[RouteStats]
    timeline: list[TimelinePoint]
