import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class UsageEvent(Base):
    """One row per request from an authenticated tenant to a real route.

    Deliberately narrow: no headers, query string, body, API key, or path (only the route
    prefix). The route prefix is copied in as text so analytics survive a route being deleted.
    """

    __tablename__ = "usage_events"
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('forwarded', 'rate_limited', 'quota_exceeded', 'rejected')",
            name="ck_usage_events_outcome",
        ),
        CheckConstraint("status_code BETWEEN 100 AND 599", name="ck_usage_events_status"),
        # Serves "this tenant's events in a time range, newest first" and keyset pagination.
        Index("ix_usage_events_tenant_time", "tenant_id", "occurred_at"),
        # Serves the same queries across all tenants (admin view).
        Index("ix_usage_events_time", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    route_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("routes.id", ondelete="SET NULL"), default=None
    )
    route_prefix: Mapped[str] = mapped_column(String(64))
    request_id: Mapped[str] = mapped_column(String(64))
    method: Mapped[str] = mapped_column(String(10))
    status_code: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(20))
    latency_ms: Mapped[float] = mapped_column(Float)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
