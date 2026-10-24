import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import Principal, get_principal
from app.core.usage_context import OUTCOMES
from app.db.session import get_session
from app.repositories import usage as usage_repo
from app.repositories.usage import UsageFilters
from app.schemas.usage import UsagePage, UsageSummary

router = APIRouter(tags=["usage"])

DEFAULT_RANGE = timedelta(hours=24)
MAX_RANGE = timedelta(days=366)
MAX_TIMELINE_BUCKETS = 1000
BUCKET_SECONDS = {"minute": 60, "hour": 3600, "day": 86400}

Outcome = Literal["forwarded", "rate_limited", "quota_exceeded", "rejected"]
assert set(OUTCOMES) == set(Outcome.__args__)  # keep the API enum and the model in step


def _aware(value: datetime) -> datetime:
    """Treat timestamps without a timezone as UTC."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _filters(
    principal: Principal,
    *,
    start: datetime | None,
    end: datetime | None,
    tenant_id: uuid.UUID | None,
    route: str | None,
    status_code: int | None,
    outcome: str | None,
) -> UsageFilters:
    end = _aware(end) if end else datetime.now(UTC)
    start = _aware(start) if start else end - DEFAULT_RANGE
    if start >= end:
        raise HTTPException(422, "'start' must be earlier than 'end'")
    if end - start > MAX_RANGE:
        raise HTTPException(422, "The time range may not exceed 366 days")
    # A tenant can only ever see its own events: the filter is not under its control.
    scoped_tenant = tenant_id if principal.is_admin else principal.tenant_id
    return UsageFilters(
        start=start,
        end=end,
        tenant_id=scoped_tenant,
        route=route,
        status_code=status_code,
        outcome=outcome,
    )


@router.get("/usage", response_model=UsagePage)
async def list_usage(
    start: datetime | None = Query(None, description="Inclusive. Default: 24 hours before 'end'."),
    end: datetime | None = Query(None, description="Exclusive. Default: now."),
    route: str | None = Query(None, description="Route prefix, e.g. 'echo'."),
    status_code: int | None = Query(None, ge=100, le=599),
    outcome: Outcome | None = None,
    tenant_id: uuid.UUID | None = Query(None, description="Admins only; ignored for tenants."),
    limit: int = Query(100, ge=1, le=500),
    cursor: str | None = Query(None, description="'next_cursor' from the previous page."),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
):
    """Individual request records, newest first, with keyset pagination."""
    filters = _filters(
        principal,
        start=start,
        end=end,
        tenant_id=tenant_id,
        route=route,
        status_code=status_code,
        outcome=outcome,
    )
    try:
        items, next_cursor = await usage_repo.list_events(
            session, filters, limit=limit, cursor=cursor
        )
    except usage_repo.InvalidCursor:
        raise HTTPException(422, "Invalid cursor") from None
    return UsagePage(items=items, next_cursor=next_cursor)


@router.get("/usage/summary", response_model=UsageSummary)
async def usage_summary(
    start: datetime | None = Query(None, description="Inclusive. Default: 24 hours before 'end'."),
    end: datetime | None = Query(None, description="Exclusive. Default: now."),
    route: str | None = Query(None, description="Route prefix, e.g. 'echo'."),
    tenant_id: uuid.UUID | None = Query(None, description="Admins only; ignored for tenants."),
    bucket: Literal["minute", "hour", "day"] = "hour",
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
):
    """Aggregate statistics.

    `accepted` = requests forwarded to an upstream; `rejected` = everything else.
    `error_rate` = 5xx responses as a share of forwarded requests.
    Latency percentiles are end-to-end at the gateway, over forwarded requests only.
    `timeline` lists only buckets that contain events.
    """
    filters = _filters(
        principal,
        start=start,
        end=end,
        tenant_id=tenant_id,
        route=route,
        status_code=None,
        outcome=None,
    )
    buckets = (filters.end - filters.start).total_seconds() / BUCKET_SECONDS[bucket]
    if buckets > MAX_TIMELINE_BUCKETS:
        raise HTTPException(
            422, "That range has too many buckets; use a larger 'bucket' or a shorter range"
        )
    data = await usage_repo.summarize(session, filters, bucket=bucket)
    return UsageSummary(start=filters.start, end=filters.end, bucket=bucket, **data)
