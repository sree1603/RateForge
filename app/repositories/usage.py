import base64
import binascii
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Select, and_, func, literal_column, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import UsageEvent

E = UsageEvent
FORWARDED = "forwarded"


@dataclass(frozen=True)
class UsageFilters:
    start: datetime
    end: datetime
    tenant_id: uuid.UUID | None = None
    route: str | None = None
    status_code: int | None = None
    outcome: str | None = None

    def conditions(self) -> list:
        conds = [E.occurred_at >= self.start, E.occurred_at < self.end]
        if self.tenant_id is not None:
            conds.append(E.tenant_id == self.tenant_id)
        if self.route is not None:
            conds.append(E.route_prefix == self.route)
        if self.status_code is not None:
            conds.append(E.status_code == self.status_code)
        if self.outcome is not None:
            conds.append(E.outcome == self.outcome)
        return conds


# ---------------- keyset pagination ----------------
class InvalidCursor(ValueError):
    pass


def encode_cursor(event: UsageEvent) -> str:
    raw = f"{event.occurred_at.isoformat()}|{event.id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        stamp, raw_id = base64.urlsafe_b64decode(padded.encode()).decode().split("|")
        occurred_at = datetime.fromisoformat(stamp)
        if occurred_at.tzinfo is None:
            raise ValueError
        return occurred_at, int(raw_id)
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise InvalidCursor("Invalid cursor") from None


async def list_events(
    session: AsyncSession, filters: UsageFilters, *, limit: int, cursor: str | None
) -> tuple[list[UsageEvent], str | None]:
    """Newest first. Pages are stable even while new events arrive, because the cursor is a
    position (time, id), not an offset."""
    stmt: Select = select(E).where(*filters.conditions())
    if cursor:
        after_time, after_id = decode_cursor(cursor)
        stmt = stmt.where(tuple_(E.occurred_at, E.id) < tuple_(after_time, after_id))
    stmt = stmt.order_by(E.occurred_at.desc(), E.id.desc()).limit(limit + 1)
    rows = list((await session.execute(stmt)).scalars())
    if len(rows) > limit:
        rows = rows[:limit]
        return rows, encode_cursor(rows[-1])
    return rows, None


# ---------------- aggregates ----------------
def _pct(fraction: str, forwarded_only):
    return (
        func.percentile_cont(literal_column(fraction))
        .within_group(E.latency_ms)
        .filter(forwarded_only)
    )


def _num(value, digits: int = 2) -> float | None:
    return None if value is None else round(float(value), digits)


async def summarize(session: AsyncSession, filters: UsageFilters, *, bucket: str) -> dict:
    """Totals, latency percentiles, top routes and a timeline for the filtered events.

    Definitions (also in the API docs):
      accepted      outcome = forwarded
      rejected      every other outcome
      error_rate    5xx responses as a share of forwarded requests (upstream failures,
                    502/504 and so on); None when nothing was forwarded
      latency       measured end to end at the gateway, over forwarded requests only, so
                    near-instant 429s do not drag the percentiles down
    """
    where = and_(*filters.conditions())
    forwarded = E.outcome == FORWARDED
    server_error = and_(forwarded, E.status_code >= 500)

    def count_if(cond):
        return func.count().filter(cond)

    totals = (
        await session.execute(
            select(
                func.count().label("total"),
                count_if(forwarded).label("accepted"),
                count_if(E.outcome == "rate_limited").label("rate_limited"),
                count_if(E.outcome == "quota_exceeded").label("quota_exceeded"),
                count_if(E.outcome == "rejected").label("other_rejected"),
                count_if(server_error).label("server_errors"),
                count_if(E.status_code.between(200, 299)).label("s2xx"),
                count_if(E.status_code.between(300, 399)).label("s3xx"),
                count_if(E.status_code.between(400, 499)).label("s4xx"),
                count_if(E.status_code >= 500).label("s5xx"),
                func.avg(E.latency_ms).filter(forwarded).label("avg"),
                _pct("0.5", forwarded).label("p50"),
                _pct("0.95", forwarded).label("p95"),
                _pct("0.99", forwarded).label("p99"),
            ).where(where)
        )
    ).one()

    route_rows = (
        await session.execute(
            select(
                E.route_prefix,
                func.count().label("requests"),
                count_if(forwarded).label("forwarded"),
                count_if(server_error).label("errors"),
                _pct("0.95", forwarded).label("p95"),
            )
            .where(where)
            .group_by(E.route_prefix)
            .order_by(func.count().desc(), E.route_prefix)
            .limit(10)
        )
    ).all()

    bucket_start = func.date_trunc(bucket, E.occurred_at, "UTC").label("bucket")
    timeline_rows = (
        await session.execute(
            select(
                bucket_start,
                func.count().label("requests"),
                count_if(E.outcome != FORWARDED).label("rejected"),
                count_if(server_error).label("errors"),
            )
            .where(where)
            .group_by(bucket_start)
            .order_by(bucket_start)
        )
    ).all()

    def rate(errors: int, forwarded_count: int) -> float | None:
        return round(errors / forwarded_count, 4) if forwarded_count else None

    return {
        "total_requests": totals.total,
        "accepted": totals.accepted,
        "rejected": {
            "total": totals.total - totals.accepted,
            "rate_limited": totals.rate_limited,
            "quota_exceeded": totals.quota_exceeded,
            "other": totals.other_rejected,
        },
        "status_classes": {
            "2xx": totals.s2xx,
            "3xx": totals.s3xx,
            "4xx": totals.s4xx,
            "5xx": totals.s5xx,
        },
        "error_rate": rate(totals.server_errors, totals.accepted),
        "latency_ms": {
            "avg": _num(totals.avg),
            "p50": _num(totals.p50),
            "p95": _num(totals.p95),
            "p99": _num(totals.p99),
        },
        "top_routes": [
            {
                "route": r.route_prefix,
                "requests": r.requests,
                "errors": r.errors,
                "error_rate": rate(r.errors, r.forwarded),
                "p95_latency_ms": _num(r.p95),
            }
            for r in route_rows
        ],
        "timeline": [
            {
                "bucket": r.bucket.astimezone(UTC),
                "requests": r.requests,
                "rejected": r.rejected,
                "errors": r.errors,
            }
            for r in timeline_rows
        ],
    }
